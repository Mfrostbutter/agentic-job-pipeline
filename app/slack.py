"""Slack webhook poster. Optional; n8n can render the same payload instead."""

from __future__ import annotations

import json
import logging

import requests

from .config import SLACK_WEBHOOK_URL

log = logging.getLogger("jobpipeline.slack")


def post_review_summary(payload: dict) -> bool:
    """Post a consolidated review+build summary to Slack.

    Returns True if posted, False if disabled or failed.
    """
    if not SLACK_WEBHOOK_URL:
        return False

    review = payload.get("review", {})
    build  = payload.get("build", {})

    lines = [
        f"*Daily job scrape: {payload.get('date', '?')}*",
        f"Scraped: {review.get('scraped', 0)}  •  "
        f"Top: {review.get('top', 0)}  •  "
        f"Second: {review.get('second', 0)}  •  "
        f"Rejected: {review.get('rejected', 0)}",
    ]

    failures = review.get("failures", 0)
    if failures:
        lines.append(f"⚠️ Review failures: {failures}")

    packages = build.get("packages") if isinstance(build, dict) else None
    packages = packages or []
    if packages:
        lines.append("\n*Packages built:*")
        for p in packages:
            lines.append(f"• {p.get('title', '?')} @ {p.get('company', '?')}  →  `{p.get('dir', '?')}`")
    elif review.get("top", 0) > 0:
        lines.append("\n_Top Tier hits found but build_packages did not run (chain_build=false or build error)._")

    text = "\n".join(lines)

    try:
        resp = requests.post(
            SLACK_WEBHOOK_URL,
            data=json.dumps({"text": text}),
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        if not resp.ok:
            log.warning("Slack post failed: %s %s", resp.status_code, resp.text[:200])
            return False
        return True
    except Exception as e:
        log.warning("Slack post exception: %s", e)
        return False
