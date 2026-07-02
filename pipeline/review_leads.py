"""review_leads.py - Haiku triage for recruiter (aging-listings) mode.

Reads the lead review queue (status='New', ai_reviewed=false) and, for each
aging posting, asks a small/cheap model to:

  - classify it as Top Tier / Second Choice / Reject as a SOURCING PROSPECT
    (can I fill this desk, or win this company as a client?), and
  - write a one-line outreach angle grounded in the recruiter's desk.

The verdict is persisted to the `leads` table. This is the cheap, automatic
leg the n8n workflow triggers; the expensive Sonnet draft (outreach_packages.py)
is gated behind a manual per-lead flag set in the UI.

Grounding comes from the profile/ directory (profile.md = the recruiter's desk
digest, profile.yaml = territory + titles). See profile_config.py.

Usage:
    python review_leads.py [--all-pending] [--limit N] [--json]

Env:
    ANTHROPIC_API_KEY   (ANTHROPIC_JOB_KEY overrides if you split billing)
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import anthropic

import leads_repo
import profile_config

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_JOB_KEY") or os.environ["ANTHROPIC_API_KEY"]
MODEL = "claude-haiku-4-5-20251001"

VALID_TIERS = {"Top Tier", "Second Choice", "Reject"}


def _desk_system() -> str:
    profile = profile_config.load_profile()
    digest = profile_config.load_digest()
    search = profile_config.search_cfg(profile)
    titles = ", ".join(search.get("target_titles", []) or []) or "(not specified)"
    location_policy = search.get("location_policy", "(not specified)")
    guidance = search.get("title_guidance", "")
    auto_reject = search.get("auto_reject_rules", []) or []
    reject_block = "\n".join(f"- {r}" for r in auto_reject) or "- (none specified)"

    return f"""You are a triage assistant for a recruiter / staffing desk. You are NOT
screening these postings for a job seeker. Each posting is a SALES LEAD: a
company that has had a role open long enough that it is likely struggling to
fill it, which makes it a prospect the recruiter can help (and win as a client).

The recruiter's desk:
{digest}

Roles this desk fills / sells into: {titles}
Territory / location policy: {location_policy}
Desk guidance: {guidance}

Auto-reject rules:
{reject_block}

For the posting you are given, decide:
  tier   one of "Top Tier", "Second Choice", "Reject".
         "Top Tier"      = strong prospect: on-desk role, in territory, aging
                           enough to signal real hiring pain, not an agency.
         "Second Choice" = plausible but weaker fit or needs a new sourcing push.
         "Reject"        = off-desk, out of territory, or an auto-reject rule hit.
  angle  ONE sentence (<= 200 chars) the recruiter could open outreach with,
         referencing the SPECIFIC role and the fact it has been open a while.
         Ground it only in what the posting and the desk digest actually say.
  reason short justification (<= 200 chars) for the tier.

Return ONLY a JSON object: {{"tier": "...", "angle": "...", "reason": "..."}}
No markdown, no code fence, no extra text."""


def _lead_user_msg(lead: dict) -> str:
    desc = (lead.get("description") or "")[:4000]
    age = lead.get("posting_age_days")
    return (
        f"Role title: {lead.get('role_title','')}\n"
        f"Company: {lead.get('company','')}\n"
        f"Location: {lead.get('location','')}\n"
        f"Workplace: {lead.get('workplace','')}\n"
        f"Posting age (days): {age}\n"
        f"Pre-scored lead score: {lead.get('lead_score')}\n"
        f"Job description:\n{desc}\n"
    )


def _parse_verdict(text: str) -> dict:
    text = (text or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"no JSON object in model output: {text[:200]}")
    obj = json.loads(text[start:end + 1])
    tier = str(obj.get("tier", "")).strip()
    if tier not in VALID_TIERS:
        # normalize common variants
        low = tier.lower()
        if "top" in low:
            tier = "Top Tier"
        elif "second" in low or "maybe" in low:
            tier = "Second Choice"
        else:
            tier = "Reject"
    return {
        "tier": tier,
        "angle": str(obj.get("angle", "")).strip()[:400],
        "reason": str(obj.get("reason", "")).strip()[:400],
    }


def review(all_pending: bool, limit: int | None) -> dict:
    queue = leads_repo.list_review_queue(all_pending=all_pending)
    if limit is not None:
        queue = queue[:limit]

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    system = _desk_system()

    reviewed = top = second = rejected = errors = 0
    for lead in queue:
        try:
            msg = client.messages.create(
                model=MODEL,
                max_tokens=1024,
                system=system,
                messages=[{"role": "user", "content": _lead_user_msg(lead)}],
            )
            text = next((b.text for b in msg.content
                         if getattr(b, "type", "") == "text" and b.text), None)
            if text is None:
                raise ValueError("model returned no text block")
            verdict = _parse_verdict(text)
            breakdown = f"[{MODEL}] {verdict['tier']}: {verdict['reason']}"
            if verdict["tier"] == "Reject":
                breakdown = f"[{MODEL}] REJECT: {verdict['reason']}"
            leads_repo.update_review(
                lead["lead_id"], tier=verdict["tier"],
                angle=verdict["angle"], breakdown=breakdown,
            )
            reviewed += 1
            if verdict["tier"] == "Top Tier":
                top += 1
            elif verdict["tier"] == "Second Choice":
                second += 1
            else:
                rejected += 1
        except Exception as e:  # noqa: BLE001 - one bad lead should not kill the batch
            errors += 1
            print(f"[error] lead {lead.get('lead_id')}: {e}", file=sys.stderr)

    return {
        "queue": len(queue),
        "reviewed": reviewed,
        "top": top,
        "second": second,
        "rejected": rejected,
        "errors": errors,
        "model": MODEL,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Haiku triage for recruiter leads")
    ap.add_argument("--all-pending", action="store_true",
                    help="Review every unreviewed New lead, not just today's.")
    ap.add_argument("--limit", type=int, default=None, help="Cap to N leads.")
    ap.add_argument("--json", action="store_true", help="Emit summary JSON to stdout.")
    args = ap.parse_args()

    result = review(all_pending=args.all_pending, limit=args.limit)

    if args.json:
        print(json.dumps(result))
    else:
        print(f"Reviewed {result['reviewed']}/{result['queue']} leads: "
              f"{result['top']} top, {result['second']} second, "
              f"{result['rejected']} reject, {result['errors']} errors.")


if __name__ == "__main__":
    main()
