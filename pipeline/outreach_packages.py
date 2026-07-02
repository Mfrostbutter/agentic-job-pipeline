"""outreach_packages.py - Sonnet outreach drafting for recruiter mode.

For every lead the recruiter flagged for outreach (build_outreach=TRUE, not yet
drafted), a stronger model writes a two-part outreach package:

  1. a short, warm LinkedIn message, and
  2. a cold email (subject + body)

both referencing the SPECIFIC long-open role and grounded only in the
recruiter's desk digest (profile/profile.md) so nothing is invented.

Each package is (a) written back to the `leads` table and (b) staged on disk as
plain text plus a per-campaign CSV and JSON that import cleanly into any email
tool or CRM. Nothing is sent; the human reviews and sends.

This is the expensive leg, gated behind the manual per-lead flag, exactly like
build_packages.py in job-search mode.

Usage:
    python outreach_packages.py [--limit N] [--json]

Env:
    ANTHROPIC_API_KEY   (ANTHROPIC_JOB_KEY overrides if you split billing)
    WRITER_MODEL        optional, default claude-sonnet-5
    OUTREACH_DIR        optional, default <repo>/out/outreach
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path

import anthropic

import leads_repo
import profile_config

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_JOB_KEY") or os.environ["ANTHROPIC_API_KEY"]
WRITER_MODEL = os.environ.get("WRITER_MODEL", "claude-sonnet-5")

_REPO_ROOT = Path(__file__).resolve().parents[1]
OUTREACH_DIR = Path(os.environ.get("OUTREACH_DIR", str(_REPO_ROOT / "out" / "outreach")))

_BANNED = ["iconic", "groundbreaking", "visionary", "cutting-edge", "game-changer",
           "revolutionary", "synergy", "leverage", "circle back"]


def _slug(*parts: str) -> str:
    raw = "-".join(p for p in parts if p)
    raw = re.sub(r"[^a-zA-Z0-9]+", "-", raw).strip("-").lower()
    return raw[:60] or "lead"


def _writer_system() -> str:
    profile = profile_config.load_profile()
    digest = profile_config.load_digest()
    candidate = profile_config.candidate(profile)
    name = candidate.get("name", "the recruiter")
    signoff = candidate.get("name", "")
    firm = candidate.get("company", "") or candidate.get("firm", "")

    return f"""You write first-touch recruiter outreach on behalf of {name}{(' at ' + firm) if firm else ''}.

The outreach targets a company that has had a specific role open for a while. A
long-open role is a real, fundable hiring need the recruiter can help fill. Your
job is warm, specific, and useful, never spammy.

The recruiter's desk (the ONLY facts you may claim about them):
{digest}

Hard rules:
- Reference the SPECIFIC role and that it has been open a while, naturally.
- Ground every claim about the recruiter's track record in the desk digest above.
  Do not invent placements, numbers, clients, or credentials.
- No em dashes. Use commas, periods, semicolons, or parentheses.
- No corporate filler. Never use: {", ".join(_BANNED)}.
- Plain, human, senior-peer voice. No "I hope this email finds you well."
- LinkedIn message: <= 600 characters, no subject line, one clear soft CTA.
- Email: a subject line <= 70 chars and a body of 90 to 140 words with a soft CTA.

Return ONLY this JSON object, no markdown, no code fence:
{{"linkedin": "...", "email_subject": "...", "email_body": "...", "signoff": "{signoff}"}}"""


def _lead_user_msg(lead: dict) -> str:
    desc = (lead.get("description") or "")[:4000]
    age = lead.get("posting_age_days")
    angle = lead.get("angle") or ""
    return (
        f"Role title: {lead.get('role_title','')}\n"
        f"Company: {lead.get('company','')}\n"
        f"Location: {lead.get('location','')}\n"
        f"Workplace: {lead.get('workplace','')}\n"
        f"Posting age (days): {age}\n"
        f"Suggested angle (from triage): {angle}\n"
        f"Contact name (may be blank): {lead.get('contact_name','')}\n"
        f"Job description:\n{desc}\n"
    )


def _parse(text: str) -> dict:
    text = (text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"no JSON object in model output: {text[:200]}")
    obj = json.loads(text[start:end + 1])
    return {
        "linkedin": str(obj.get("linkedin", "")).strip(),
        "email_subject": str(obj.get("email_subject", "")).strip(),
        "email_body": str(obj.get("email_body", "")).strip(),
    }


def _stage_files(campaign_dir: Path, lead: dict, pkg: dict) -> Path:
    slug = _slug(lead.get("company", ""), lead.get("role_title", ""))
    lead_dir = campaign_dir / slug
    lead_dir.mkdir(parents=True, exist_ok=True)
    (lead_dir / "linkedin.txt").write_text(pkg["linkedin"] + "\n", encoding="utf-8")
    (lead_dir / "email.txt").write_text(
        f"Subject: {pkg['email_subject']}\n\n{pkg['email_body']}\n", encoding="utf-8")
    return lead_dir


def _write_campaign_exports(campaign_dir: Path, rows: list[dict]) -> None:
    """Per-campaign CSV + JSON, importable into any email tool or CRM."""
    if not rows:
        return
    cols = ["lead_id", "company", "role_title", "location", "source_url",
            "posting_age_days", "lead_score", "contact_name", "contact_handle",
            "linkedin_message", "email_subject", "email_body"]
    with open(campaign_dir / "outreach.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})
    (campaign_dir / "outreach.json").write_text(
        json.dumps(rows, indent=2, default=str), encoding="utf-8")


def run(limit: int | None) -> dict:
    queue = leads_repo.list_outreach_queue()
    if limit is not None:
        queue = queue[:limit]

    if not queue:
        return {"eligible": 0, "drafted": 0, "failed": 0, "leads": [],
                "failures": [], "model": WRITER_MODEL,
                "note": "No leads flagged for outreach."}

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    system = _writer_system()

    drafted = 0
    done: list[dict] = []
    failures: list[dict] = []
    # group staged exports by campaign label
    exports: dict[str, list[dict]] = {}

    for lead in queue:
        try:
            msg = client.messages.create(
                model=WRITER_MODEL,
                max_tokens=1500,
                system=system,
                messages=[{"role": "user", "content": _lead_user_msg(lead)}],
            )
            text = next((b.text for b in msg.content
                         if getattr(b, "type", "") == "text" and b.text), None)
            if text is None:
                raise ValueError("model returned no text block")
            pkg = _parse(text)
            if not pkg["linkedin"] or not pkg["email_body"]:
                raise ValueError("model returned empty outreach")

            campaign = lead.get("campaign_label") or "unlabeled"
            campaign_dir = OUTREACH_DIR / _slug(campaign)
            lead_dir = _stage_files(campaign_dir, lead, pkg)

            leads_repo.save_outreach(
                lead["lead_id"], linkedin=pkg["linkedin"],
                email_subject=pkg["email_subject"], email_body=pkg["email_body"],
                package_path=str(lead_dir),
            )
            exports.setdefault(campaign, []).append({
                "lead_id": lead["lead_id"],
                "company": lead.get("company", ""),
                "role_title": lead.get("role_title", ""),
                "location": lead.get("location", ""),
                "source_url": lead.get("source_url", ""),
                "posting_age_days": lead.get("posting_age_days"),
                "lead_score": lead.get("lead_score"),
                "contact_name": lead.get("contact_name", ""),
                "contact_handle": lead.get("contact_handle", ""),
                "linkedin_message": pkg["linkedin"],
                "email_subject": pkg["email_subject"],
                "email_body": pkg["email_body"],
            })
            drafted += 1
            done.append({"role_title": lead.get("role_title", ""),
                         "company": lead.get("company", ""),
                         "path": str(lead_dir)})
        except Exception as e:  # noqa: BLE001
            failures.append({"lead_id": lead.get("lead_id"), "error": str(e)})
            print(f"[error] lead {lead.get('lead_id')}: {e}", file=sys.stderr)

    for campaign, rows in exports.items():
        _write_campaign_exports(OUTREACH_DIR / _slug(campaign), rows)

    return {
        "eligible": len(queue),
        "drafted": drafted,
        "failed": len(failures),
        "leads": done,
        "failures": failures,
        "staged_dir": str(OUTREACH_DIR),
        "model": WRITER_MODEL,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Sonnet outreach drafting for recruiter leads")
    ap.add_argument("--limit", type=int, default=None, help="Cap to N leads.")
    ap.add_argument("--json", action="store_true", help="Emit summary JSON to stdout.")
    args = ap.parse_args()

    result = run(limit=args.limit)

    if args.json:
        print(json.dumps(result))
    else:
        print(f"Drafted {result['drafted']}/{result.get('eligible', 0)} outreach "
              f"packages ({result['failed']} failed). Staged under {result.get('staged_dir')}")


if __name__ == "__main__":
    main()
