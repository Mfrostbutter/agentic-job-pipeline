"""Build application packages for qualifying jobs.

For each qualifying job:
  1. Researcher call (Haiku)  - emits a 3-field JSON brief via tool-use
  2. Writer call (Sonnet)     - drafts resume.md from brief + your profile
  3. Critic call (Haiku)      - scores the draft on 4 dimensions
  4. Revisor call (Sonnet)    - targeted revision, only if the critic flags weak spots
  5. Saves files to           <applications>/YYYY-MM-DD-{Co}-{Role}/
  6. Renders PDF              via render_resume.py
  7. Updates Postgres         application_package_created=true, package_path=<dir>

Eligibility: build_package=true AND application_package_created=false.

  You tick Build Package on rows you want packages for after triaging the
  review output. Nothing auto-builds; every build is opt-in, so you control
  API spend.

All personal content (contact block, background digest, templates, style
rules) comes from the profile/ directory; see profile/profile.example.yaml.

Usage:
    python build_packages.py [--dry-run] [--limit N] [--no-pdf] [--json]

    --dry-run      List qualifying jobs, no API calls or writes
    --limit N      Process at most N jobs
    --no-pdf       Skip PDF rendering
    --json         Emit summary JSON to stdout (for the service)

Env vars (or .env in this directory):
    ANTHROPIC_API_KEY   (ANTHROPIC_JOB_KEY overrides if you split billing)
    WRITER_MODEL        optional, default claude-sonnet-5
    PG_*                Postgres connection (see seen_jobs_db.py)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
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

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_JOB_KEY") or os.environ["ANTHROPIC_API_KEY"]

import jobs_repo  # Postgres data layer (same dir, on sys.path)
import profile_config

# Record-shape field keys (plain names; the record dict mirrors the jobs row)
F_TITLE       = "title"
F_COMPANY     = "company"
F_LOCATION    = "location"
F_SOURCE_URL  = "source_url"
F_SCORE       = "relevance_score"
F_WORKPLACE   = "workplace"
F_SAL_MIN     = "salary_min"
F_SAL_MAX     = "salary_max"
F_DESC        = "description"
F_TIER        = "tier"
F_BUILD_PKG   = "build_package"
F_PKG_CREATED = "application_package_created"
F_PKG_PATH    = "package_path"

RESEARCHER_MODEL = "claude-haiku-4-5-20251001"
WRITER_MODEL     = os.environ.get("WRITER_MODEL", "claude-sonnet-5")

RENDER_SCRIPT    = Path(__file__).resolve().parent / "render_resume.py"
APPLICATIONS_DIR = Path(os.environ.get(
    "APPLICATIONS_DIR",
    str(Path(__file__).resolve().parents[1] / "data" / "applications")))

JD_MAX_CHARS = 3_000


# ---------------------------------------------------------------------------
# Researcher rubric + tool schema
# ---------------------------------------------------------------------------

def build_researcher_rubric(profile: dict) -> str:
    name = profile_config.candidate(profile).get("name", "the candidate")
    banned = profile_config.style_cfg(profile).get("banned_phrases") or []
    banned_line = ", ".join(str(b) for b in banned) if banned else "corporate PR cliches"
    return f"""
You are the Job Hunt Resume Researcher for {name}'s search.

Given a job posting and {name}'s profile digest, extract three things for the writer to use.

## Rules

- `signal_phrases`: 5 to 8 verbatim phrases pulled directly from the JD. Pick phrases the writer must echo naturally in the resume or cover letter (skills, responsibilities, outcomes, technologies the JD highlights). No paraphrasing.
- `proof_points`: 2 to 3 entries. Each has a `claim` (one short sentence about something {name} has done that maps to the JD) and a `proof_source` (which line/section of the profile digest it comes from). Pull ONLY from the profile digest, never invent.
- `positioning_theme`: one sentence the writer will use to anchor the cover letter and email. Strategic framing, no jargon.

NEVER:
- Invent metrics, companies, dates, or accomplishments
- Use banned phrases ({banned_line})
- Claim experience the profile digest does not show

Call the `emit_brief` tool with the three fields.
"""


BRIEF_TOOL = {
    "name": "emit_brief",
    "description": "Emit the 3-field role brief for the writer.",
    "input_schema": {
        "type": "object",
        "properties": {
            "signal_phrases": {
                "type": "array",
                "minItems": 5,
                "maxItems": 8,
                "items": {"type": "string"},
                "description": "5-8 verbatim phrases from the JD the writer must echo.",
            },
            "proof_points": {
                "type": "array",
                "minItems": 2,
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "properties": {
                        "claim": {"type": "string", "description": "One sentence about the candidate's experience that maps to the JD."},
                        "proof_source": {"type": "string", "description": "Where in the profile digest this comes from."},
                    },
                    "required": ["claim", "proof_source"],
                },
                "description": "2-3 proof points traceable to the profile digest.",
            },
            "positioning_theme": {
                "type": "string",
                "description": "One sentence anchoring the cover letter and email.",
            },
        },
        "required": ["signal_phrases", "proof_points", "positioning_theme"],
    },
}


# ---------------------------------------------------------------------------
# Writer system prompt (cached)
# ---------------------------------------------------------------------------

def build_writer_system(profile: dict, profile_digest: str,
                        resume_tpl: str, resume_example: str) -> list[dict]:
    """Multi-block system prompt with cache_control on stable sections.

    The writer's single deliverable is `resume.md`. Cover letters are generated
    separately (quick-add path) or written by hand; drafting in your own voice
    usually beats a generated letter for the roles you care most about.
    """
    name = profile_config.candidate(profile).get("name", "The Candidate")
    contact_md = profile_config.contact_line_md(profile)
    style = profile_config.style_cfg(profile)
    banned = [str(b) for b in (style.get("banned_phrases") or [])]
    banned_line = ", ".join(banned) if banned else "(none configured)"
    scrub_dashes = bool(style.get("scrub_em_dashes", True))

    dash_rule = ""
    if scrub_dashes:
        dash_rule = """
**HARD RULE: NO EM DASHES ANYWHERE.**
The character "—" (U+2014) and " -- " are banned without exception. Replace every em dash with a comma, semicolon, colon, or rephrase. Dashes are only valid inside numeric ranges (e.g. 2021-2024, $240K-$325K).
"""

    tpl_section = f"\n## Resume Template\n\n{resume_tpl}\n" if resume_tpl.strip() else ""
    example_section = (f"\n## Gold Standard Example\n\n### resume.md example:\n{resume_example}\n"
                       if resume_example.strip() else "")

    stable = f"""You are the Job Hunt Resume Writer for {name}'s search.

Given a 3-field role brief plus job metadata, produce exactly ONE file: `resume.md`.

## Contact Block (use verbatim)

# {name}

{contact_md}

## Profile Digest (the ONLY source of factual claims)

{profile_digest}

## Resume Voice (HARD)

- Lead with what the work accomplishes, not the implementation stack
- Quantified outcomes on every bullet where possible
- Verbs up front, outcomes trailing
- Name a tool only when it is load-bearing to the bullet's outcome
- Structure: Summary, then a "Selected Work" section (named projects, each
  leading with the outcome), then a "Core Competencies" section grouping
  skills under 3-4 labeled lines, then Experience, then Education & Credentials
- "Core Competencies" is a curated skills section, NOT an inventory dump.
  List capabilities and named technologies grouped by theme.
{dash_rule}
FORBIDDEN in resume content:
- Banned phrases: {banned_line}
- Operational trivia (port numbers, RAM/CPU specs, config-file names)
- Any claim that does not trace back to the profile digest
{example_section}{tpl_section}
## Output Format

Output ONE fenced code block with the filename as the tag:

```resume.md
[resume content]
```

Do not emit cover-letter.md, email-body.md, or any other file. Resume only.

## Critical Rules

- NEVER fabricate roles, companies, dates, degrees, certifications, titles, or metrics
- Each resume bullet must lead with a verb
- Mirror the JD's language via the brief's signal phrases, naturally, never as a keyword dump"""

    return [{"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}}]


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

@dataclass
class BuildSummary:
    eligible: int = 0
    built: int = 0
    failed: int = 0
    packages: list[dict] = field(default_factory=list)   # {title, company, dir, record_id}
    failures: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "eligible": self.eligible,
            "built": self.built,
            "failed": self.failed,
            "packages": self.packages,
            "failures": self.failures,
        }


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _row_to_record(row: dict) -> dict:
    """Adapt a jobs_repo row into the record shape process_job consumes:
    {id: job_id, fields: {...}}."""
    return {
        "id": row["job_id"],
        "fields": {
            F_TITLE:       row.get("title"),
            F_COMPANY:     row.get("company"),
            F_LOCATION:    row.get("location"),
            F_SOURCE_URL:  row.get("source_url"),
            F_WORKPLACE:   row.get("workplace"),
            F_SAL_MIN:     row.get("salary_min"),
            F_SAL_MAX:     row.get("salary_max"),
            F_DESC:        row.get("description"),
            F_TIER:        row.get("tier"),
            F_BUILD_PKG:   row.get("build_package"),
            F_PKG_CREATED: row.get("application_package_created"),
            F_PKG_PATH:    row.get("package_path"),
        },
    }


def fetch_eligible() -> list[dict]:
    """Record-shaped dicts where build_package is set and no package built yet.

    Build is gated entirely on the human ticking Build Package after manual
    triage; untick the flag to skip a row.
    """
    return [_row_to_record(r) for r in jobs_repo.list_eligible_for_build()]


def _mark_done(job_id: str, pkg_path: str) -> None:
    """Stamp the row as built and clear the build flag so it leaves the build queue."""
    try:
        if not jobs_repo.mark_built(job_id, pkg_path):
            print(f"    [warn] jobs update affected 0 rows for job_id={job_id}")
    except Exception as e:
        print(f"    [warn] jobs update failed: {e}")
        return

    try:
        from seen_jobs_db import mark_status_by_job_id
        mark_status_by_job_id(job_id, "built")
    except Exception as e:
        print(f"    [warn] seen_jobs sync failed: {e}", file=sys.stderr)


class BuildGateError(Exception):
    """Raised by build_one_job when a row is not eligible to build."""
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _load_context() -> tuple[dict, str, str, str]:
    profile = profile_config.load_profile()
    digest = profile_config.load_digest()
    resume_tpl = profile_config.read_optional("resume-template.md")
    resume_example = profile_config.read_optional("resume-example.md")
    return profile, digest, resume_tpl, resume_example


def build_one_job(job_id: str, *, no_pdf: bool = False, quick: bool = False) -> dict:
    """Build a single package for one job_id (the single-build path in the UI).

    Mirrors run_build's per-row pipeline but scoped to one row. Raises
    BuildGateError when the row is missing / not flagged / already built.

    quick=True (manual-add path): also generates and renders a cover letter."""
    row = jobs_repo.get_raw(job_id)
    if row is None:
        raise BuildGateError("not_found", f"job {job_id} not found")
    if not row.get("build_package"):
        raise BuildGateError("build_not_flagged", "Set Build Package=true before triggering a build.")
    if row.get("application_package_created"):
        raise BuildGateError("already_built",
                             "Application Package Created is already true; flip it off to rebuild.")

    profile, digest, resume_tpl, resume_example = _load_context()
    researcher_system = [
        {"type": "text",
         "text": f"# Candidate Profile Digest\n\n{digest}\n\n{build_researcher_rubric(profile)}",
         "cache_control": {"type": "ephemeral"}},
    ]
    writer_system = build_writer_system(profile, digest, resume_tpl, resume_example)
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    ok, pkg_dir = process_job(client, _row_to_record(row), researcher_system,
                              writer_system, profile,
                              no_pdf=no_pdf, verbose=False,
                              keep_files={"resume.md"} if quick else None,
                              render_files=("resume.md",))
    if not ok:
        return {"built": False, "error": "process_job returned failure"}

    # quick (manual-add) path also generates + renders a tailored cover letter.
    if quick:
        try:
            _build_cover_letter(client, pkg_dir, profile, digest, row, no_pdf=no_pdf)
        except Exception as e:  # noqa: BLE001, resume already built; CL is best-effort
            print(f"    [warn] cover letter generation failed: {e}")

    return {"built": True, "package_path": str(pkg_dir)}


def build_cover_letter_system(profile: dict) -> str:
    name = profile_config.candidate(profile).get("name", "the candidate")
    c = profile_config.candidate(profile)
    header_bits = "  ·  ".join(b for b in (c.get("location", ""), c.get("email", ""),
                                           str(c.get("phone", ""))) if b)
    style = profile_config.style_cfg(profile)
    banned = ", ".join(str(b) for b in (style.get("banned_phrases") or [])) or "corporate filler"
    dash_rule = ('- NO em dashes (U+2014) or " -- ". Use commas, periods, semicolons, or rephrase.\n'
                 if style.get("scrub_em_dashes", True) else "")
    return f"""You write a cover letter for {name} for a specific role, in THEIR voice: direct, concrete, warm but not effusive. Ground every claim in the profile digest and the tailored resume provided. Never invent experience, employers, metrics, or dates.

Rules:
- 250 to 350 words. Three or four short paragraphs. One page.
- Open with why this specific role and company, not a generic hook.
- Body: 2 to 3 concrete, relevant proof points drawn from the resume/profile.
- Close with a direct, confident call to talk.
- First person. Plain language. No banned phrases ({banned}).
{dash_rule}- Output ONE fenced code block tagged cover-letter.md.

Format the top of the letter as:
{name}
{header_bits}

Dear Hiring Team,
"""


def _build_cover_letter(client: anthropic.Anthropic, pkg_dir: Path, profile: dict,
                        profile_digest: str, row: dict, *, no_pdf: bool) -> None:
    """Generate cover-letter.md grounded in the profile + tailored resume + JD,
    then render cover-letter.pdf. Used by the quick (manual-add) build path."""
    company = row.get("company") or ""
    title = row.get("title") or ""
    location = row.get("location") or ""
    jd = (row.get("description") or "")[:JD_MAX_CHARS]
    resume_md = _read(pkg_dir / "resume.md")[:6000]

    system = [
        {"type": "text", "text": f"# Candidate Profile Digest\n\n{profile_digest}",
         "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": build_cover_letter_system(profile)},
    ]
    user = (f"Company: {company}\nRole: {title}\nLocation: {location}\n\n"
            f"# Tailored resume (mirror its framing and proof points)\n{resume_md}\n\n"
            f"# Job description (truncated)\n{jd or '[none]'}\n\n"
            f"Write the cover letter now.")
    msg = client.messages.create(model=WRITER_MODEL, max_tokens=1500,
                                 system=system, messages=[{"role": "user", "content": user}])
    raw = msg.content[0].text
    parsed = _parse_files(raw)
    content = parsed.get("cover-letter.md") or raw
    if profile_config.style_cfg(profile).get("scrub_em_dashes", True):
        content = _scrub_em_dashes(content)
    cl_path = pkg_dir / "cover-letter.md"
    cl_path.write_text(content + "\n", encoding="utf-8")

    if not no_pdf and RENDER_SCRIPT.exists():
        try:
            subprocess.run([sys.executable, str(RENDER_SCRIPT), str(cl_path),
                            str(cl_path.with_suffix(".pdf"))],
                           capture_output=True, text=True, timeout=60)
        except Exception as e:  # noqa: BLE001
            print(f"    [warn] cover letter PDF render failed: {e}")


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return f"[FILE NOT FOUND: {path}]"


def _slugify(s: str, max_len: int = 30) -> str:
    s = re.sub(r"[^a-zA-Z0-9\s-]", "", s)
    s = re.sub(r"\s+", "-", s.strip())
    s = re.sub(r"-{2,}", "-", s)
    return s[:max_len].strip("-")


def _format_salary(sal_min: float | None, sal_max: float | None) -> str:
    if sal_min and sal_max:
        return f"${int(sal_min):,} to ${int(sal_max):,}"
    if sal_min:
        return f"${int(sal_min):,}+"
    if sal_max:
        return f"up to ${int(sal_max):,}"
    return "not stated"


def _parse_files(response_text: str) -> dict[str, str]:
    pattern = r"```([\w.\-]+)\n([\s\S]*?)```"
    blocks = re.findall(pattern, response_text)
    return {name.strip(): content.strip() for name, content in blocks}


def _scrub_em_dashes(content: str) -> str:
    """Replace em dashes (and ASCII fallback / en-dash-as-em-dash) with commas.

    Defensive post-process; models routinely produce em dashes even when the
    system prompt forbids them. En dashes inside numeric ranges (no
    surrounding spaces, e.g. 2021-2024) are preserved. Toggle via
    style.scrub_em_dashes in profile.yaml.
    """
    if not content:
        return content
    content = content.replace(" — ", ", ")
    content = content.replace("—", ", ")
    content = content.replace(" -- ", ", ")
    content = content.replace(" – ", ", ")
    return content


def _scrub_warnings(content: str) -> bool:
    """Check whether content still contains a banned dash variant."""
    return ("—" in content) or (" -- " in content) or (" – " in content)


def _basic_checks(files: dict[str, str], banned_phrases: list[str]) -> list[str]:
    warnings: list[str] = []
    resume = files.get("resume.md", "")
    if resume and _scrub_warnings(resume):
        warnings.append("  [warn] em / en dash detected in resume.md")
    if resume and banned_phrases:
        rx = re.compile("|".join(rf"\b{re.escape(p)}\b" for p in banned_phrases), re.I)
        if rx.search(resume):
            warnings.append("  [warn] banned phrase in resume.md")
    return warnings


# ---------------------------------------------------------------------------
# Per-job pipeline
# ---------------------------------------------------------------------------

def _call_researcher(client: anthropic.Anthropic, system_blocks: list[dict],
                     user_content: str) -> dict | None:
    try:
        msg = client.messages.create(
            model=RESEARCHER_MODEL,
            max_tokens=2048,
            system=system_blocks,
            tools=[BRIEF_TOOL],
            tool_choice={"type": "tool", "name": "emit_brief"},
            messages=[{"role": "user", "content": user_content}],
        )
    except Exception as e:
        print(f"    [error] Researcher call failed: {e}")
        return None
    for block in msg.content:
        if block.type == "tool_use" and block.name == "emit_brief":
            return block.input
    return None


def _call_writer(client: anthropic.Anthropic, system_blocks: list[dict],
                 user_content: str) -> str | None:
    try:
        msg = client.messages.create(
            model=WRITER_MODEL,
            max_tokens=8192,
            system=system_blocks,
            messages=[{"role": "user", "content": user_content}],
        )
    except Exception as e:
        print(f"    [error] Writer call failed: {e}")
        return None
    return msg.content[0].text


# ---------------------------------------------------------------------------
# Critic + Revisor (iterative quality loop)
# ---------------------------------------------------------------------------

CRITIC_MODEL  = "claude-haiku-4-5-20251001"
REVISOR_MODEL = os.environ.get("WRITER_MODEL", "claude-sonnet-5")
CRITIC_DIMS   = ["ats_coverage", "voice_consistency", "factual_grounding", "banned_phrases"]
CRITIC_THRESHOLD = 7  # any dim below this triggers a revise pass


def build_critic_rubric(profile: dict) -> str:
    name = profile_config.candidate(profile).get("name", "the candidate")
    style = profile_config.style_cfg(profile)
    banned = ", ".join(str(b) for b in (style.get("banned_phrases") or [])) or "corporate PR cliches"
    dash_bit = 'em-dashes (— or " -- "), ' if style.get("scrub_em_dashes", True) else ""
    return f"""You are a brutally honest resume critic for {name}'s job search.

You will be given:
  1. The job description (truncated)
  2. The role brief (signal phrases, proof points, positioning theme)
  3. A first-draft resume.md the writer just produced

Score the draft on 4 dimensions (integer 1-10 each):

  ats_coverage      How well does the resume incorporate the JD's must-have
                    keywords and skill phrases naturally? 10 = covers every
                    critical keyword in context. 1 = misses obvious ones.

  voice_consistency Does the resume body use a high-level outcome voice?
                    10 = clean outcomes, verbs lead, no inventory dumps.
                    1 = port numbers / spec trivia leak in, or "I shipped X"
                    first-person leaks.

  factual_grounding Does every claim trace to the profile digest?
                    10 = nothing fabricated. 1 = invented roles, metrics,
                    titles, dates, or certifications.

  banned_phrases    Free of {dash_bit}and banned phrases ({banned})?
                    10 = clean. 1 = multiple violations.

Then populate `weak_dimensions` with the names of any dimension scoring below
7. This drives whether a revise pass runs. Leave empty if all dimensions are
>= 7.

In `notes`, give the revisor a 1-3 sentence directive: what to keep, what to
fix. Be specific.

Call the record_critique tool with your verdict. Do not free-form."""


CRITIC_TOOL = {
    "name": "record_critique",
    "description": "Record the critique verdict for this resume draft.",
    "input_schema": {
        "type": "object",
        "properties": {
            "ats_coverage":      {"type": "integer", "minimum": 1, "maximum": 10},
            "voice_consistency": {"type": "integer", "minimum": 1, "maximum": 10},
            "factual_grounding": {"type": "integer", "minimum": 1, "maximum": 10},
            "banned_phrases":    {"type": "integer", "minimum": 1, "maximum": 10},
            "weak_dimensions":   {
                "type": "array",
                "items": {"type": "string", "enum": CRITIC_DIMS},
                "description": "Dimensions scoring below 7; the revisor targets these.",
            },
            "notes":             {"type": "string", "maxLength": 1200},
        },
        "required": [*CRITIC_DIMS, "weak_dimensions", "notes"],
    },
}


def _call_critic(client: anthropic.Anthropic, critic_rubric: str, resume_md: str,
                 jd_text: str, brief_md: str) -> dict | None:
    """Haiku critic. Returns a structured verdict or None on failure."""
    user_content = (
        f"# Job Description (truncated)\n\n{jd_text[:JD_MAX_CHARS]}\n\n"
        f"# Role Brief\n\n{brief_md}\n\n"
        f"# Draft Resume\n\n{resume_md}\n\n"
        f"Call record_critique with your verdict."
    )
    try:
        msg = client.messages.create(
            model=CRITIC_MODEL,
            max_tokens=1024,
            system=[{"type": "text", "text": critic_rubric,
                     "cache_control": {"type": "ephemeral"}}],
            tools=[CRITIC_TOOL],
            tool_choice={"type": "tool", "name": "record_critique"},
            messages=[{"role": "user", "content": user_content}],
        )
    except Exception as e:
        print(f"    [error] Critic call failed: {e}")
        return None
    for block in msg.content:
        if block.type == "tool_use" and block.name == "record_critique":
            data = dict(block.input)
            # Defensive: enforce the threshold rule even if the model picked
            # weak_dimensions inconsistently with its scores.
            weak = [d for d in CRITIC_DIMS if data.get(d, 10) < CRITIC_THRESHOLD]
            data["weak_dimensions"] = weak
            return data
    return None


def _call_revisor(client: anthropic.Anthropic, writer_system: list[dict],
                  resume_md: str, verdict: dict, jd_text: str,
                  brief_md: str) -> str | None:
    """Sonnet revisor. Targets only the dimensions the critic flagged as weak.

    Returns the revised resume.md content, or None on failure. Uses the same
    writer system prompt as the original draft (so all hard rules carry
    through), then asks for a targeted revision in user content.
    """
    weak = ", ".join(verdict.get("weak_dimensions", []))
    user_content = (
        f"# Job Description (truncated)\n\n{jd_text[:JD_MAX_CHARS]}\n\n"
        f"# Role Brief\n\n{brief_md}\n\n"
        f"# Current Draft (needs revision)\n\n{resume_md}\n\n"
        f"# Critic verdict\n\n"
        f"Scores: " + ", ".join(f"{d}={verdict.get(d)}" for d in CRITIC_DIMS) + "\n"
        f"Weak dimensions: {weak}\n"
        f"Notes from critic: {verdict.get('notes', '').strip()}\n\n"
        f"# Your task\n\n"
        f"Revise the resume to fix the weak dimensions above. Keep everything "
        f"the critic did not flag. Output the full revised file in one fenced "
        f"```resume.md ... ``` block. Do not emit any other files."
    )
    try:
        msg = client.messages.create(
            model=REVISOR_MODEL,
            max_tokens=8192,
            system=writer_system,
            messages=[{"role": "user", "content": user_content}],
        )
    except Exception as e:
        print(f"    [error] Revisor call failed: {e}")
        return None
    raw = msg.content[0].text
    parsed = _parse_files(raw)
    return parsed.get("resume.md") or raw


def process_job(client: anthropic.Anthropic, record: dict,
                researcher_system: list[dict], writer_system: list[dict],
                profile: dict,
                no_pdf: bool, verbose: bool,
                keep_files: set[str] | None = None,
                render_files: tuple[str, ...] = ("resume.md",)) -> tuple[bool, Path | None]:
    """keep_files limits which generated .md files are saved (None = all).
    render_files lists which saved .md files are rendered to PDF."""
    fields = record.get("fields", {})
    title    = fields.get(F_TITLE, "Unknown Role")
    company  = fields.get(F_COMPANY, "Unknown Company")
    location = fields.get(F_LOCATION, "")
    url      = fields.get(F_SOURCE_URL, "")
    workplace = fields.get(F_WORKPLACE, "")
    sal_min  = fields.get(F_SAL_MIN)
    sal_max  = fields.get(F_SAL_MAX)
    jd_text  = (fields.get(F_DESC, "") or "")[:JD_MAX_CHARS]
    tier     = fields.get(F_TIER, "")
    salary_str = _format_salary(sal_min, sal_max)

    style = profile_config.style_cfg(profile)
    scrub = bool(style.get("scrub_em_dashes", True))
    banned_phrases = [str(b) for b in (style.get("banned_phrases") or [])]

    if verbose:
        print(f"\n  [{tier or 'manual'}] {title} @ {company}")

    # --- Researcher ---
    researcher_user = f"""Job metadata:
Title: {title}
Company: {company}
Location: {location}
Workplace: {workplace}
Salary: {salary_str}
URL: {url}

Job Description (truncated to {JD_MAX_CHARS} chars):
{jd_text or '[No description available]'}

Call emit_brief with the 3-field brief."""

    if verbose:
        print("    [1/5] Researcher (Haiku)...")
    brief = _call_researcher(client, researcher_system, researcher_user)
    if brief is None:
        return False, None

    # --- Writer ---
    brief_md = (
        "## Signal Phrases\n" + "\n".join(f"- {p}" for p in brief["signal_phrases"]) + "\n\n"
        "## Proof Points\n" + "\n".join(f"- {p['claim']}  _(source: {p['proof_source']})_"
                                        for p in brief["proof_points"]) + "\n\n"
        f"## Positioning Theme\n{brief['positioning_theme']}"
    )

    writer_user = f"""Role brief:

{brief_md}

Job metadata:
Title: {title}
Company: {company}
Location: {location}
Salary: {salary_str}
URL: {url}

Today's date: {datetime.now().strftime('%Y-%m-%d')}

Produce the resume.md file now."""

    if verbose:
        print(f"    [2/5] Writer ({WRITER_MODEL})...")
    raw_output = _call_writer(client, writer_system, writer_user)
    if raw_output is None:
        return False, None

    # --- Parse ---
    files = _parse_files(raw_output)
    if "resume.md" not in files:
        if verbose:
            print("    [error] Writer did not emit resume.md")
        return False, None

    if scrub:
        files = {name: _scrub_em_dashes(content) for name, content in files.items()}

    # --- Critic (Haiku) ---
    if verbose:
        print("    [3/5] Critic (Haiku)...")
    critic_rubric = build_critic_rubric(profile)
    verdict = _call_critic(client, critic_rubric, files["resume.md"], jd_text, brief_md)
    if verdict and verbose:
        scores = ", ".join(f"{d}={verdict[d]}" for d in CRITIC_DIMS)
        print(f"      verdict: {scores}, weak={verdict.get('weak_dimensions', [])}")

    # --- Revise (Sonnet, conditional) ---
    if verdict and verdict.get("weak_dimensions"):
        if verbose:
            print(f"    [4/5] Revising weak dimensions: {verdict['weak_dimensions']}")
        revised = _call_revisor(client, writer_system, files["resume.md"],
                                verdict, jd_text, brief_md)
        if revised:
            files["resume.md"] = _scrub_em_dashes(revised) if scrub else revised
        elif verbose:
            print("      [warn] Revisor returned nothing; keeping original")
    else:
        if verbose:
            print("    [4/5] Revise skipped (all dimensions strong)")

    for w in _basic_checks(files, banned_phrases):
        if verbose:
            print(w)

    # --- Save ---
    today = datetime.now().strftime("%Y-%m-%d")
    dir_name = f"{today}-{_slugify(company)}-{_slugify(title)}"
    pkg_dir = APPLICATIONS_DIR / dir_name
    pkg_dir.mkdir(parents=True, exist_ok=True)

    if verbose:
        print(f"    [4/5] Saving to {dir_name}/")
    for fname, content in files.items():
        if fname.endswith(".md") and (keep_files is None or fname in keep_files):
            (pkg_dir / fname).write_text(content + "\n", encoding="utf-8")

    # --- PDF(s) ---
    if no_pdf:
        if verbose:
            print("    [5/5] PDF skipped (--no-pdf)")
    elif RENDER_SCRIPT.exists():
        if verbose:
            print(f"    [5/5] Rendering PDF(s): {', '.join(render_files)}")
        for md_name in render_files:
            md_path = pkg_dir / md_name
            if not md_path.exists():
                continue
            pdf_path = md_path.with_suffix(".pdf")
            try:
                result = subprocess.run(
                    [sys.executable, str(RENDER_SCRIPT), str(md_path), str(pdf_path)],
                    capture_output=True, text=True, timeout=60
                )
                if result.returncode != 0 and verbose:
                    print(f"    [warn] PDF render error ({md_name}): {result.stderr[:200]}")
            except subprocess.TimeoutExpired:
                if verbose:
                    print(f"    [warn] PDF render timed out ({md_name})")
            except Exception as e:
                if verbose:
                    print(f"    [warn] PDF render failed ({md_name}): {e}")

    _mark_done(record["id"], str(pkg_dir))
    if verbose:
        print(f"    Done: {pkg_dir}")
    return True, pkg_dir


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_build(
    *,
    dry_run: bool = False,
    limit: int | None = None,
    no_pdf: bool = False,
    verbose: bool = True,
) -> BuildSummary:
    summary = BuildSummary()

    if verbose:
        print("Fetching eligible jobs from Postgres...")
    records = fetch_eligible()
    summary.eligible = len(records)
    if verbose:
        print(f"  Found {len(records)} eligible jobs")

    if limit:
        records = records[:limit]
        if verbose:
            print(f"  Limited to {len(records)}")

    if not records:
        return summary

    if dry_run:
        if verbose:
            print("\n[dry-run] Jobs that would build:")
            for r in records:
                f = r.get("fields", {})
                print(f"  {f.get(F_TIER, 'manual') or 'manual':12s}  "
                      f"{f.get(F_TITLE, '?')} @ {f.get(F_COMPANY, '?')}")
        return summary

    # Load static context once
    if verbose:
        print("\nLoading profile + templates...")
    profile, digest, resume_tpl, resume_example = _load_context()

    researcher_system = [
        {"type": "text",
         "text": f"# Candidate Profile Digest\n\n{digest}\n\n{build_researcher_rubric(profile)}",
         "cache_control": {"type": "ephemeral"}},
    ]
    writer_system = build_writer_system(profile, digest, resume_tpl, resume_example)

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    if verbose:
        print(f"Building {len(records)} packages "
              f"(researcher={RESEARCHER_MODEL}, writer={WRITER_MODEL})...")

    for i, rec in enumerate(records, 1):
        fields = rec.get("fields", {})
        title   = fields.get(F_TITLE, "?")
        company = fields.get(F_COMPANY, "?")
        url     = fields.get(F_SOURCE_URL, "")
        if verbose:
            print(f"\n[{i}/{len(records)}] {title} @ {company}")
        ok, pkg_dir = process_job(client, rec, researcher_system, writer_system,
                                  profile, no_pdf, verbose)
        if ok:
            summary.built += 1
            summary.packages.append({
                "title": title, "company": company,
                "url": url, "record_id": rec["id"],
                "dir": str(pkg_dir),
            })
        else:
            summary.failed += 1
            summary.failures.append({
                "title": title, "company": company,
                "url": url, "record_id": rec["id"],
            })
        time.sleep(1)

    if verbose:
        print("\n=== Build complete ===")
        print(f"  Eligible: {summary.eligible}")
        print(f"  Built:    {summary.built}")
        print(f"  Failed:   {summary.failed}")

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Build application packages for eligible jobs.")
    parser.add_argument("--dry-run", action="store_true",
                        help="List eligible jobs, no API calls or writes")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process at most N jobs")
    parser.add_argument("--no-pdf", action="store_true",
                        help="Skip PDF rendering (markdown only)")
    parser.add_argument("--json", action="store_true",
                        help="Emit summary as JSON to stdout (for piping)")
    args = parser.parse_args()

    summary = run_build(
        dry_run=args.dry_run,
        limit=args.limit,
        no_pdf=args.no_pdf,
        verbose=not args.json,
    )

    if args.json:
        print(json.dumps(summary.to_dict(), indent=2))


if __name__ == "__main__":
    main()
