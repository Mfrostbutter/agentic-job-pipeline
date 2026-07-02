"""Triage new job postings with Claude Haiku and tag them in Postgres.

Query: Status='New' AND ai_reviewed=false AND created today (local time).

Per record, makes one Haiku call with a cached system prompt (your profile
digest + a rubric built from profile/profile.yaml). Output is forced to strict
JSON via tool-use. Writes tier / ai_reviewed / breakdown back to the jobs
table. Returns a summary dict suitable for downstream Slack rendering.

A deterministic pre-classifier runs first and costs nothing: batch-duplicate
clustering, location reject/demote rules, hard-reject title regexes,
preferred-company auto-promotion, and a low-score demotion. Everything it
touches never reaches the paid AI call. All the knobs live in
profile/profile.yaml; the code stays generic.

Can be invoked as a CLI or imported by the FastAPI service.

Usage (CLI):
    python review_new_jobs.py [--dry-run] [--limit N] [--all-pending] [--json]

    --dry-run       Print per-job verdicts, no writes
    --limit N       Stop after processing N records (useful for smoke tests)
    --all-pending   Drop the today-only filter; classify every Status=New record
                    that's still unreviewed. Use after a scraper backlog day.
    --json          Emit summary as JSON to stdout (for the service)

Requires env vars (or .env in this directory):
    ANTHROPIC_API_KEY   (ANTHROPIC_JOB_KEY overrides if you split billing)
    PG_*                Postgres connection (see seen_jobs_db.py)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import anthropic
from dotenv import load_dotenv

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

load_dotenv(Path(__file__).parent / ".env")

import jobs_repo  # Postgres data layer (same dir, on sys.path)
import profile_config

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_JOB_KEY") or os.environ["ANTHROPIC_API_KEY"]

# Record-shape field keys (plain names; the record dict mirrors the jobs row)
F_TITLE       = "title"
F_COMPANY     = "company"
F_LOCATION    = "location"
F_SOURCE_URL  = "source_url"
F_WORKPLACE   = "workplace"
F_STATUS      = "status"
F_SCORE       = "relevance_score"
F_BREAKDOWN   = "breakdown"
F_SAL_MIN     = "salary_min"
F_SAL_MAX     = "salary_max"
F_DESC        = "description"
F_TIER        = "tier"
F_AI_REVIEWED = "ai_reviewed"
F_AI_OVERRIDE     = "ai_override"
F_OVERRIDE_REASON = "override_reason"

MODEL = "claude-haiku-4-5-20251001"
JD_MAX_CHARS = 5_000

TIER_TOP    = "Top Tier"
TIER_SECOND = "Second Choice"
TIER_REJECT = "Reject"


# ---------------------------------------------------------------------------
# Rubric (built from profile.yaml so the engine stays generic)
# ---------------------------------------------------------------------------

def build_rubric(profile: dict) -> str:
    cand   = profile_config.candidate(profile)
    search = profile_config.search_cfg(profile)
    name = cand.get("name", "the candidate")
    salary_floor = int(search.get("salary_floor") or 0)
    target_titles = [str(t) for t in (search.get("target_titles") or [])]
    title_guidance = str(search.get("title_guidance") or "").strip()
    location_policy = str(search.get("location_policy") or "").strip()
    reject_rules = [str(r) for r in (search.get("auto_reject_rules") or [])]
    preferred = [str(c) for c in (search.get("preferred_companies") or [])]

    lines = [
        f"You are {name}'s job-triage assistant. For each job description, classify it as one of:",
        "",
        f'- "top"    -> strong fit. Title, compensation, geography, and content all align with {name}\'s search. Worth a fully tailored application package.',
        '- "second" -> plausible fit with one or two soft mismatches (e.g. comp band unknown but title strong). Worth keeping in the backlog for manual review.',
        '- "reject" -> not pursuing. Below floor, wrong role type, wrong seniority, or hard exclusion.',
        "",
        "## Target titles",
        "",
        "The titles being pursued:",
    ]
    lines += [f"- {t}" for t in target_titles] or ["- (none configured; judge on the JD body)"]
    if title_guidance:
        lines += ["", "Title guidance:", title_guidance]

    lines += ["", "## Geography", "", location_policy or "No location constraints configured."]

    lines += ["", "## Reject criteria, any one of these means reject", ""]
    if salary_floor:
        lines.append(f"- Compensation band stated and entirely below ${salary_floor:,} base")
    lines += [f"- {r}" for r in reject_rules]
    if not reject_rules and not salary_floor:
        lines.append("- (no hard reject rules configured; use judgment)")

    if preferred:
        lines += ["", "## Preferred companies", "",
                  "Lean toward top for postings from: " + ", ".join(preferred) + "."]

    floor_str = f"${salary_floor:,}" if salary_floor else "the configured floor"
    lines += [
        "",
        "## Hard rules",
        "",
        f"- Judge fit against {name}'s profile digest above; never assume experience the digest does not show.",
        "- Use the Relevance Score as one signal but apply judgment. A low heuristic score with a strong on-target title can still be top.",
        "- Be skeptical of junior/entry-level titles even when the company is hot.",
        "",
        "## Output",
        "",
        "You must call the `record_verdict` tool with exactly one verdict. The `reason` field is a single short sentence (max 140 chars) explaining the call. Use `salary_red_flag=true` only if the JD explicitly shows a band below " + floor_str + " base.",
    ]
    return "\n".join(lines)


VERDICT_TOOL = {
    "name": "record_verdict",
    "description": "Record the triage verdict for this job posting.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["top", "second", "reject"],
                "description": "The triage verdict.",
            },
            "reason": {
                "type": "string",
                "maxLength": 140,
                "description": "One short sentence (max 140 chars) explaining the call.",
            },
            "salary_red_flag": {
                "type": "boolean",
                "description": "True only if the JD explicitly shows a comp band below the configured salary floor.",
            },
        },
        "required": ["verdict", "reason", "salary_red_flag"],
    },
}


# ---------------------------------------------------------------------------
# Pre-classification layer (deterministic filters before the Haiku call)
# All patterns come from profile.yaml; empty lists disable a filter.
# ---------------------------------------------------------------------------

def _compile_list(patterns) -> list[re.Pattern]:
    out = []
    for p in patterns or []:
        try:
            out.append(re.compile(str(p), re.IGNORECASE))
        except re.error as e:
            print(f"[warn] bad regex in profile.yaml: {p!r} ({e})", file=sys.stderr)
    return out


class PreClassifier:
    def __init__(self, profile: dict):
        s = profile_config.search_cfg(profile)
        self.hard_reject_title = _compile_list(s.get("hard_reject_title_patterns"))
        self.reject_location   = _compile_list(s.get("reject_location_patterns"))
        self.demote_location   = _compile_list(s.get("demote_location_patterns"))
        self.location_override = _compile_list(s.get("location_override_patterns"))
        self.low_score_threshold = s.get("low_score_threshold")
        self.promote_tokens = [str(t).lower() for t in (s.get("promote_tokens") or [])]
        self.preferred_companies = {str(c).lower().strip()
                                    for c in (s.get("preferred_companies") or [])}
        self.target_titles = [str(t).lower() for t in (s.get("target_titles") or [])]

    def hard_reject_title_reason(self, title: str) -> str | None:
        t = (title or "").lower()
        for rx in self.hard_reject_title:
            if rx.search(t):
                return f"Title matches hard-reject pattern: {rx.pattern}"
        return None

    def _location_overridden(self, location: str) -> bool:
        loc = (location or "").lower()
        return any(rx.search(loc) for rx in self.location_override)

    def reject_location_reason(self, location: str) -> str | None:
        loc = (location or "").lower()
        if self._location_overridden(loc):
            return None
        for rx in self.reject_location:
            if rx.search(loc):
                return f"Location out of policy: matches {rx.pattern}"
        return None

    def demote_location_reason(self, location: str) -> str | None:
        loc = (location or "").lower()
        if self._location_overridden(loc):
            return None
        for rx in self.demote_location:
            if rx.search(loc):
                return f"Location demoted to Second: matches {rx.pattern}"
        return None

    def auto_top_reason(self, title: str, company: str) -> str | None:
        c = (company or "").lower().strip()
        if c not in self.preferred_companies:
            return None
        t = (title or "").lower()
        if any(tt in t for tt in self.target_titles):
            return f"Auto-Top: {company} (preferred company) + target title"
        return None

    def low_score_demote(self, title: str, company: str, score) -> str | None:
        if self.low_score_threshold is None:
            return None
        try:
            score_num = float(score) if score not in (None, "", "n/a") else None
        except (TypeError, ValueError):
            score_num = None
        if score_num is None or score_num >= float(self.low_score_threshold):
            return None
        t = (title or "").lower()
        if any(tok in t for tok in self.promote_tokens):
            return None
        if (company or "").lower().strip() in self.preferred_companies:
            return None
        return "Low score + no promote signal"


# ---------------------------------------------------------------------------
# Salary extraction from JD prose (generic; no config needed)
# ---------------------------------------------------------------------------

_SAL_RANGE_PATTERNS = [
    # $180,000 - $250,000 / $180,000 to $250,000 (with optional USD prefix/suffix)
    re.compile(
        r"(?:USD\s*)?\$?\s*(\d{2,3}(?:,\d{3})+)\s*(?:USD\s*)?(?:-|to|–|—)\s*\$?\s*(\d{2,3}(?:,\d{3})+)(?:\s*USD)?",
        re.IGNORECASE,
    ),
    # $180K - $250K / $180k-$250k / USD 180K - 250K
    re.compile(
        r"(?:USD\s*)?\$?\s*(\d{2,4})\s*[Kk]\s*(?:-|to|–|—)\s*\$?\s*(\d{2,4})\s*[Kk]",
        re.IGNORECASE,
    ),
    # $180-$250K shorthand: only second value has the K, first is bare number with $.
    re.compile(
        r"\$\s*(\d{2,4})\s*(?:-|to|–|—)\s*\$?\s*(\d{2,4})\s*[Kk]\b",
        re.IGNORECASE,
    ),
]

# Restrictive single-value: only fire if the value is adjacent to base/anchor/target
# language. Otherwise we'd grab equity grants, signing bonuses, etc.
_SAL_SINGLE_CONTEXT_RE = re.compile(
    r"(base(?:\s+salary)?|target\s+base|anchor|salary\s+of)\s*[:\-]?\s*\$\s*(\d{2,4})\s*[Kk]\b"
    r"|"
    r"\$\s*(\d{2,4})\s*[Kk]\s+base(?:\s+salary)?\b"
    r"|"
    r"(base(?:\s+salary)?|target\s+base|anchor|salary\s+of)\s*[:\-]?\s*\$\s*(\d{2,3}(?:,\d{3})+)\b"
    r"|"
    r"\$\s*(\d{2,3}(?:,\d{3})+)\s+base(?:\s+salary)?\b",
    re.IGNORECASE,
)


def _to_dollars(raw: str, k_suffix: bool) -> int | None:
    try:
        n = int(raw.replace(",", ""))
    except (ValueError, AttributeError):
        return None
    if k_suffix:
        n *= 1000
    return n


def _plausible_band(lo: int, hi: int) -> bool:
    if lo > hi:
        return False
    if lo < 30_000 or hi > 1_000_000:
        return False
    if lo > 0 and (hi / lo) > 5.0:
        return False
    return True


def _plausible_single(v: int) -> bool:
    return 30_000 <= v <= 1_000_000


def _extract_salary_from_jd(text: str) -> tuple[int | None, int | None]:
    """Lift a salary band out of JD prose. Returns (min, max) in whole dollars, or
    (None, None) if no plausible match."""
    if not text:
        return (None, None)

    # 1. Comma-form range
    for m in _SAL_RANGE_PATTERNS[0].finditer(text):
        lo = _to_dollars(m.group(1), k_suffix=False)
        hi = _to_dollars(m.group(2), k_suffix=False)
        if lo is not None and hi is not None and _plausible_band(lo, hi):
            return (lo, hi)

    # 2. K-suffix range
    for m in _SAL_RANGE_PATTERNS[1].finditer(text):
        lo = _to_dollars(m.group(1), k_suffix=True)
        hi = _to_dollars(m.group(2), k_suffix=True)
        if lo is not None and hi is not None and _plausible_band(lo, hi):
            return (lo, hi)

    # 3. Shorthand $180-$250K (apply K to both)
    for m in _SAL_RANGE_PATTERNS[2].finditer(text):
        lo = _to_dollars(m.group(1), k_suffix=True)
        hi = _to_dollars(m.group(2), k_suffix=True)
        if lo is not None and hi is not None and _plausible_band(lo, hi):
            return (lo, hi)

    # 4. Single value with explicit base/anchor/target context
    for m in _SAL_SINGLE_CONTEXT_RE.finditer(text):
        groups = m.groups()
        raw_k = groups[1] or groups[2]
        raw_comma = groups[4] or groups[5]
        if raw_k:
            v = _to_dollars(raw_k, k_suffix=True)
        elif raw_comma:
            v = _to_dollars(raw_comma, k_suffix=False)
        else:
            v = None
        if v is not None and _plausible_single(v):
            return (v, v)

    return (None, None)


# ---------------------------------------------------------------------------
# Batch-duplicate clustering (same role posted across many locations)
# ---------------------------------------------------------------------------

def _jd_fingerprint(desc: str) -> str:
    """First 200 chars of normalized JD body, sha256 hex (first 12 chars)."""
    body = " ".join((desc or "")[:200].lower().split())
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]


def _cluster_key(title: str, company: str, location: str, desc: str) -> tuple:
    t = " ".join((title or "").lower().split()[:3])
    c = (company or "").lower().strip()
    fp = _jd_fingerprint(desc)
    loc = (location or "").lower().split(",")[0].strip()
    return (c, t, loc, fp)


def _mark_clusters(records: list[dict]) -> dict[str, tuple[str, str]]:
    """Return {record_id: (status, canonical_label)} where status is 'canonical'
    or 'sibling'. Canonical = highest relevance score; tiebreak = earliest created."""
    by_key: dict[tuple, list[dict]] = {}
    for rec in records:
        f = rec.get("fields", {})
        key = _cluster_key(
            f.get(F_TITLE, ""), f.get(F_COMPANY, ""),
            f.get(F_LOCATION, ""), f.get(F_DESC, ""),
        )
        by_key.setdefault(key, []).append(rec)
    status: dict[str, tuple[str, str]] = {}
    for key, group in by_key.items():
        if len(group) <= 1:
            continue
        def _rank(rec):
            f = rec.get("fields", {})
            score = f.get(F_SCORE, 0) or 0
            created = rec.get("createdTime", "")
            return (-float(score), created)
        group_sorted = sorted(group, key=_rank)
        canonical = group_sorted[0]
        canonical_title = canonical.get("fields", {}).get(F_TITLE, "Unknown")
        canonical_company = canonical.get("fields", {}).get(F_COMPANY, "Unknown")
        canonical_label = f"{canonical_title} @ {canonical_company}"
        status[canonical["id"]] = ("canonical", canonical_label)
        for sib in group_sorted[1:]:
            status[sib["id"]] = ("sibling", canonical_label)
    return status


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Verdict:
    verdict: str          # top | second | reject
    reason: str
    salary_red_flag: bool
    extracted_sal_min: int | None = None
    extracted_sal_max: int | None = None


@dataclass
class ReviewSummary:
    scraped: int = 0
    top: int = 0
    second: int = 0
    rejected: int = 0
    failures: int = 0
    top_jobs: list[dict] = field(default_factory=list)   # {title, company, url, record_id}
    second_jobs: list[dict] = field(default_factory=list)
    failed_jobs: list[dict] = field(default_factory=list)
    pre_classifier_counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "scraped": self.scraped,
            "top": self.top,
            "second": self.second,
            "rejected": self.rejected,
            "failures": self.failures,
            "top_jobs": self.top_jobs,
            "second_jobs": self.second_jobs,
            "failed_jobs": self.failed_jobs,
            "pre_classifier_counts": self.pre_classifier_counts,
        }


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _row_to_record(row: dict) -> dict:
    """Adapt a jobs_repo row into the record shape the rest of this module
    consumes: {id, createdTime, fields:{...}}."""
    created = row.get("created_at")
    return {
        "id": row["job_id"],
        "createdTime": created.isoformat() if created is not None else "",
        "fields": {
            F_TITLE:       row.get("title"),
            F_COMPANY:     row.get("company"),
            F_LOCATION:    row.get("location"),
            F_SOURCE_URL:  row.get("source_url"),
            F_WORKPLACE:   row.get("workplace"),
            F_STATUS:      row.get("status"),
            F_SCORE:       row.get("relevance_score"),
            F_BREAKDOWN:   row.get("breakdown"),
            F_SAL_MIN:     row.get("salary_min"),
            F_SAL_MAX:     row.get("salary_max"),
            F_DESC:        row.get("description"),
            F_TIER:        row.get("tier"),
            F_AI_REVIEWED: row.get("ai_reviewed"),
            F_AI_OVERRIDE: row.get("ai_override"),
            F_OVERRIDE_REASON: row.get("override_reason"),
        },
    }


def fetch_queue(all_pending: bool = False) -> list[dict]:
    """Fetch records that need review (Status='New', not yet AI-reviewed)."""
    return [_row_to_record(r) for r in jobs_repo.list_review_queue(all_pending=all_pending)]


def fetch_corrections(limit: int = 20) -> list[dict]:
    """Pull recent records where the human overrode an AI REJECT verdict.

    Two signals, in priority order:
      1. Explicit: ai_override=true. Set by the web UI when the reviewer moves
         Reject -> Top/Second; they may also supply override_reason.
      2. Implicit fallback: Tier=Top/Second AND breakdown contains `] REJECT:`.

    Explicit first, implicit fills the remaining slots, deduped by job_id.
    """
    return [_row_to_record(r) for r in jobs_repo.list_corrections(limit=limit)]


_REJECT_REASON_RE = re.compile(r"\[AI \d{4}-\d{2}-\d{2}\] REJECT:\s*(.+?)(?:\n|$)")


def build_corrections_block(records: list[dict]) -> str:
    """Format human overrides as a few-shot block for the Haiku system prompt.
    Returns "" if no corrections; the caller then skips the second cache block.
    """
    if not records:
        return ""
    lines = [
        "## False-reject corrections (human overrides, most recent)",
        "",
        "These are jobs that the AI originally classified as REJECT, but the",
        "human reviewer kept in the Top or Second pile. Treat them as labeled",
        "examples of rules that were too strict. For each one below, the AI's",
        "original reject reason is shown alongside the JD snippet and the",
        "final tier.",
        "",
        "When you encounter a new job that resembles one of these (similar title",
        "shape, similar company class, similar JD content), DO NOT reject for",
        "the same reason that fired here. Lean toward Top or Second instead.",
        "",
    ]
    for i, rec in enumerate(records, 1):
        f = rec.get("fields", {})
        title = f.get(F_TITLE, "?")
        company = f.get(F_COMPANY, "?")
        location = f.get(F_LOCATION, "?") or "?"
        tier = f.get(F_TIER, "?")
        breakdown = f.get(F_BREAKDOWN, "") or ""
        m = _REJECT_REASON_RE.search(breakdown)
        ai_reason = m.group(1).strip() if m else "(reason not parseable)"
        jd_snippet = " ".join((f.get(F_DESC, "") or "")[:280].split())
        override_reason = (f.get(F_OVERRIDE_REASON) or "").strip()
        explicit = bool(f.get(F_AI_OVERRIDE))
        block = (
            f"Example {i}:\n"
            f"  Title:    {title}\n"
            f"  Company:  {company}\n"
            f"  Location: {location}\n"
            f"  AI's original reject reason: {ai_reason}\n"
        )
        if override_reason:
            block += f"  Reviewer said: actually keep it, because {override_reason}\n"
        elif explicit:
            block += "  Reviewer explicitly flagged this as an AI false-reject\n"
        block += (
            f"  Final tier: {tier}\n"
            f"  JD opening: {jd_snippet}..."
        )
        lines.append(block)
    lines.append("")
    lines.append(
        f"(Total corrections in corpus: {len(records)}. Apply pattern recognition, "
        "not literal matching.)"
    )
    return "\n\n".join(lines)


def write_verdict(
    record_id: str,
    verdict: Verdict,
    existing_breakdown: str,
    extracted_min: int | None = None,
    extracted_max: int | None = None,
) -> bool:
    """Patch a single record with the verdict."""
    if verdict.verdict == "top":
        tier_value = TIER_TOP
    elif verdict.verdict == "second":
        tier_value = TIER_SECOND
    else:
        tier_value = TIER_REJECT

    today = datetime.now().strftime("%Y-%m-%d")
    review_note = f"[AI {today}] {verdict.verdict.upper()}: {verdict.reason}"
    new_breakdown = (existing_breakdown + "\n" + review_note) if existing_breakdown else review_note

    try:
        ok = jobs_repo.update_review(
            record_id,
            tier=tier_value,
            breakdown=new_breakdown,
            salary_min=extracted_min,
            salary_max=extracted_max,
        )
        if not ok:
            print(f"    [warn] jobs update affected 0 rows for job_id={record_id}")
            return False
    except Exception as e:
        print(f"    [warn] jobs update failed: {e}")
        return False

    try:
        from seen_jobs_db import mark_status_by_job_id
        pg_status = "reject" if verdict.verdict == "reject" else "new"
        mark_status_by_job_id(record_id, pg_status, reason=verdict.reason)
    except Exception as e:
        print(f"    [warn] seen_jobs sync failed: {e}", file=sys.stderr)

    return True


# ---------------------------------------------------------------------------
# Claude call
# ---------------------------------------------------------------------------

def _format_salary(sal_min: float | None, sal_max: float | None) -> str:
    if sal_min and sal_max:
        return f"${int(sal_min):,} to ${int(sal_max):,}"
    if sal_min:
        return f"${int(sal_min):,}+"
    if sal_max:
        return f"up to ${int(sal_max):,}"
    return "not stated"


def build_system_blocks(profile_digest: str, rubric: str,
                        corrections_text: str = "") -> list[dict]:
    """Build the cached system prompt blocks.

    Block 1: Profile + Rubric. Cache anchor #1, stable across days.
    Block 2 (optional): False-reject corrections from human overrides.
    Separate cache anchor so corrections can rotate daily without busting the
    block-1 cache.
    """
    blocks: list[dict] = [
        {
            "type": "text",
            "text": f"# Candidate Profile Digest\n\n{profile_digest}\n\n{rubric}",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    if corrections_text:
        blocks.append({
            "type": "text",
            "text": corrections_text,
            "cache_control": {"type": "ephemeral"},
        })
    return blocks


def classify_one(
    client: anthropic.Anthropic,
    record: dict,
    system_blocks: list[dict],
    pre: PreClassifier,
    cluster_status: dict[str, tuple[str, str]] | None = None,
    counter: dict[str, int] | None = None,
) -> Verdict | None:
    fields = record.get("fields", {})
    title    = fields.get(F_TITLE, "Unknown")
    company  = fields.get(F_COMPANY, "Unknown")
    location = fields.get(F_LOCATION, "")
    workplace = fields.get(F_WORKPLACE, "")
    score    = fields.get(F_SCORE, "n/a")
    sal_min  = fields.get(F_SAL_MIN)
    sal_max  = fields.get(F_SAL_MAX)
    jd_text  = (fields.get(F_DESC, "") or "")[:JD_MAX_CHARS]

    def _bump(key: str) -> None:
        if counter is not None:
            counter[key] = counter.get(key, 0) + 1

    # Salary regex extractor: only run when structured salary is missing.
    extracted_min: int | None = None
    extracted_max: int | None = None
    if not sal_min and not sal_max:
        extracted_min, extracted_max = _extract_salary_from_jd(jd_text)
        if extracted_min is not None or extracted_max is not None:
            _bump("salary_extracted")

    eff_sal_min = sal_min if sal_min else extracted_min
    eff_sal_max = sal_max if sal_max else extracted_max

    def _attach(v: Verdict) -> Verdict:
        v.extracted_sal_min = extracted_min
        v.extracted_sal_max = extracted_max
        return v

    # --- Pre-classification layer (deterministic, before Haiku) ---

    # 1. Cluster sibling? Force Second, skip Haiku.
    if cluster_status is not None:
        cs = cluster_status.get(record.get("id", ""))
        if cs and cs[0] == "sibling":
            _bump("cluster_sibling")
            return _attach(Verdict("second", f"Cluster sibling of {cs[1]}"[:140], False))

    # 2. Reject-location rule? Force Reject.
    geo_reject = pre.reject_location_reason(location)
    if geo_reject:
        _bump("geo_reject")
        return _attach(Verdict("reject", geo_reject[:140], False))

    # 3. Demote-location rule? Force Second.
    geo_demote = pre.demote_location_reason(location)
    if geo_demote:
        _bump("geo_demote")
        return _attach(Verdict("second", geo_demote[:140], False))

    # 4. Hard-reject title regex? Force Reject.
    title_reject = pre.hard_reject_title_reason(title)
    if title_reject:
        _bump("hard_reject_title")
        return _attach(Verdict("reject", title_reject[:140], False))

    # 5. Preferred company + target title? Force Top.
    auto_top = pre.auto_top_reason(title, company)
    if auto_top:
        _bump("auto_top_preferred")
        return _attach(Verdict("top", auto_top[:140], False))

    # 6. Low heuristic score with no promote token? Force Second.
    demote = pre.low_score_demote(title, company, score)
    if demote:
        _bump("score_blend_low")
        return _attach(Verdict("second", demote, False))

    # --- End pre-classification; fall through to Haiku ---

    user_content = f"""Job posting to triage:

Title: {title}
Company: {company}
Location: {location}
Workplace: {workplace or 'not stated'}
Salary: {_format_salary(eff_sal_min, eff_sal_max)}
Relevance Score (heuristic from the scraper, treat as one signal among many): {score}

Job Description (truncated to {JD_MAX_CHARS} chars):
{jd_text or '[No description available]'}

Call the record_verdict tool with your decision."""

    try:
        msg = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=system_blocks,
            tools=[VERDICT_TOOL],
            tool_choice={"type": "tool", "name": "record_verdict"},
            messages=[{"role": "user", "content": user_content}],
        )
    except Exception as e:
        print(f"    [error] Anthropic call failed: {e}")
        return None

    for block in msg.content:
        if block.type == "tool_use" and block.name == "record_verdict":
            data = block.input
            try:
                return _attach(Verdict(
                    verdict=data["verdict"],
                    reason=data["reason"][:140],
                    salary_red_flag=bool(data.get("salary_red_flag", False)),
                ))
            except (KeyError, TypeError) as e:
                print(f"    [error] Bad tool input: {e}  raw={data}")
                return None
    print("    [error] No tool_use block in response")
    return None


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_review(
    *,
    dry_run: bool = False,
    limit: int | None = None,
    all_pending: bool = False,
    verbose: bool = True,
) -> ReviewSummary:
    """Main entry. Used by CLI and the FastAPI service."""
    summary = ReviewSummary()

    if verbose:
        print(f"Fetching review queue (all_pending={all_pending})...")
    records = fetch_queue(all_pending=all_pending)
    summary.scraped = len(records)
    if verbose:
        print(f"  Queue size: {len(records)}")

    if limit:
        records = records[:limit]
        if verbose:
            print(f"  Limited to {len(records)} (--limit)")

    if not records:
        if verbose:
            print("Nothing to review.")
        return summary

    if dry_run and not verbose:
        return summary

    profile = profile_config.load_profile()
    profile_digest = profile_config.load_digest()
    rubric = build_rubric(profile)
    pre = PreClassifier(profile)
    corrections    = fetch_corrections(limit=20)
    corrections_text = build_corrections_block(corrections)
    if verbose and corrections:
        print(f"  Loaded {len(corrections)} false-reject corrections for few-shot prompt")
    system_blocks  = build_system_blocks(profile_digest, rubric, corrections_text)
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    # Pre-pass: mark clusters across the whole fetch so siblings can resolve
    # to their canonical title.
    cluster_status = _mark_clusters(records)
    summary.pre_classifier_counts = {}

    for i, rec in enumerate(records, 1):
        fields = rec.get("fields", {})
        title   = fields.get(F_TITLE, "?")
        company = fields.get(F_COMPANY, "?")
        url     = fields.get(F_SOURCE_URL, "")
        score   = fields.get(F_SCORE, "?")

        if verbose:
            print(f"\n[{i}/{len(records)}] {title} @ {company} (score={score})")

        verdict = classify_one(client, rec, system_blocks, pre,
                               cluster_status=cluster_status,
                               counter=summary.pre_classifier_counts)
        if verdict is None:
            summary.failures += 1
            summary.failed_jobs.append({"title": title, "company": company, "url": url,
                                        "record_id": rec["id"]})
            continue

        if verbose:
            flag = " [salary below floor]" if verdict.salary_red_flag else ""
            print(f"    -> {verdict.verdict.upper()}: {verdict.reason}{flag}")

        if verdict.verdict == "top":
            summary.top += 1
            summary.top_jobs.append({"title": title, "company": company, "url": url,
                                     "record_id": rec["id"]})
        elif verdict.verdict == "second":
            summary.second += 1
            summary.second_jobs.append({"title": title, "company": company, "url": url,
                                        "record_id": rec["id"]})
        else:
            summary.rejected += 1

        if not dry_run:
            existing_breakdown = fields.get(F_BREAKDOWN, "") or ""
            ok = write_verdict(
                rec["id"], verdict, existing_breakdown,
                extracted_min=verdict.extracted_sal_min,
                extracted_max=verdict.extracted_sal_max,
            )
            if not ok:
                summary.failures += 1

        time.sleep(0.25)  # gentle throttle

    if verbose:
        print("\n=== Review complete ===")
        print(f"  Scraped:  {summary.scraped}")
        print(f"  Top:      {summary.top}")
        print(f"  Second:   {summary.second}")
        print(f"  Rejected: {summary.rejected}")
        print(f"  Failures: {summary.failures}")
        if summary.pre_classifier_counts:
            print("  Pre-classifier counts:")
            for k in sorted(summary.pre_classifier_counts):
                print(f"    {k}: {summary.pre_classifier_counts[k]}")
            total_pre = sum(summary.pre_classifier_counts.values())
            haiku_called = max(summary.scraped - total_pre, 0)
            print(f"    (total pre-classified: {total_pre}, Haiku-called: {haiku_called})")

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Triage today's new jobs with Claude Haiku.")
    parser.add_argument("--dry-run", action="store_true", help="Print verdicts, no writes")
    parser.add_argument("--limit", type=int, default=None, help="Stop after N records")
    parser.add_argument("--all-pending", action="store_true",
                        help="Drop today-only filter; review every unreviewed Status=New record")
    parser.add_argument("--json", action="store_true",
                        help="Emit summary as JSON to stdout (for piping)")
    args = parser.parse_args()

    summary = run_review(
        dry_run=args.dry_run,
        limit=args.limit,
        all_pending=args.all_pending,
        verbose=not args.json,
    )

    if args.json:
        print(json.dumps(summary.to_dict(), indent=2))


if __name__ == "__main__":
    main()
