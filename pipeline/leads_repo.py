"""leads_repo.py - Postgres data-access layer for recruiter (aging-listings) mode.

System of record for lead rows (the `leads` table). Mirrors jobs_repo's shape
and reuses seen_jobs_db's connection settings. A lead is an aging job posting
treated as a sourcing/outreach prospect.

The flat dict carries both `lead_id` and `record_id` (same value) so a recruiter
UI can key on `record_id` exactly like the job UI does.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

import psycopg2.extras

import seen_jobs_db as _db

_BASE_COLS = (
    "lead_id, role_title, company, location, source_url, workplace, status, "
    "lead_score, posting_age_days, campaign_label, tier, angle, ai_reviewed, "
    "build_outreach, outreach_created, contact_name, contact_handle, "
    "package_path, breakdown, stage, stage_updated_at, ai_override, "
    "override_reason, created_at"
)

PATCHABLE_FIELDS: dict[str, tuple[str, type]] = {
    "tier":            ("tier", str),
    "build_outreach":  ("build_outreach", bool),
    "status":          ("status", str),
    "ai_reviewed":     ("ai_reviewed", bool),
    "ai_override":     ("ai_override", bool),
    "override_reason": ("override_reason", str),
    "stage":           ("stage", str),
    "contact_name":    ("contact_name", str),
    "contact_handle":  ("contact_handle", str),
}

TIER_VALUES_ALLOWED = {"Top Tier", "Second Choice", "Reject", ""}
STATUS_VALUES_ALLOWED = {"New", "Contacted", "Replied", "Won", "Lost", "Archived", ""}
STAGE_VALUES_ALLOWED = {"", "Contacted", "Replied", "Meeting", "Proposal", "Won", "Lost"}


def _num(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, Decimal):
        f = float(v)
        return int(f) if f.is_integer() else f
    return v


def flatten(row: dict) -> dict:
    """Translate a leads row into the flat dict a recruiter UI consumes."""
    return {
        "record_id": row["lead_id"],
        "lead_id":   row["lead_id"],
        "created_at": row["created_at"],
        "role_title": row["role_title"] or "",
        "company":    row["company"] or "",
        "location":   row["location"] or "",
        "workplace":  row["workplace"] or "",
        "url":        row["source_url"] or "",
        "tier":       row["tier"] or "",
        "status":     row["status"] or "",
        "score":      _num(row["lead_score"]),
        "posting_age_days": _num(row["posting_age_days"]),
        "campaign":   row["campaign_label"] or "",
        "angle":      row["angle"] or "",
        "ai_reviewed":      bool(row["ai_reviewed"]),
        "build_outreach":   bool(row["build_outreach"]),
        "outreach_created": bool(row["outreach_created"]),
        "ai_override":      bool(row["ai_override"]),
        "override_reason":  row["override_reason"] or "",
        "was_ai_rejected":  "] REJECT:" in (row["breakdown"] or ""),
        "contact_name":     row["contact_name"] or "",
        "contact_handle":   row["contact_handle"] or "",
        "package_path":     row["package_path"] or "",
        "stage":            row["stage"] or "",
        "stage_updated_at": row["stage_updated_at"],
    }


def _cursor(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def list_leads(*, tier: str = "all", status: str = "all",
               campaign: str = "all", limit: int = 100) -> list[dict]:
    clauses: list[str] = []
    params: list[Any] = []
    if tier and tier.lower() != "all":
        clauses.append("tier = %s")
        params.append(tier)
    if status and status.lower() != "all":
        clauses.append("status = %s")
        params.append(status)
    if campaign and campaign.lower() != "all":
        clauses.append("campaign_label = %s")
        params.append(campaign)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    # Aging leads: oldest/most-persistent first is the useful default.
    sql = (f"SELECT {_BASE_COLS} FROM leads {where} "
           f"ORDER BY lead_score DESC, posting_age_days DESC NULLS LAST LIMIT %s")
    params.append(limit)
    with _db._conn() as c, _cursor(c) as cur:
        cur.execute(sql, params)
        return [flatten(dict(r)) for r in cur.fetchall()]


def get_lead(lead_id: str) -> Optional[dict]:
    sql = (f"SELECT {_BASE_COLS}, description, outreach_linkedin, "
           f"outreach_email_subject, outreach_email_body FROM leads WHERE lead_id = %s")
    with _db._conn() as c, _cursor(c) as cur:
        cur.execute(sql, (lead_id,))
        row = cur.fetchone()
    if not row:
        return None
    base = flatten(dict(row))
    base["description"]  = row["description"] or ""
    base["breakdown"]    = row["breakdown"] or ""
    base["outreach"] = {
        "linkedin":      row["outreach_linkedin"] or "",
        "email_subject": row["outreach_email_subject"] or "",
        "email_body":    row["outreach_email_body"] or "",
    }
    return base


def get_raw(lead_id: str) -> Optional[dict]:
    with _db._conn() as c, _cursor(c) as cur:
        cur.execute("SELECT * FROM leads WHERE lead_id = %s", (lead_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def patch_lead(lead_id: str, patch: dict[str, Any]) -> Optional[dict]:
    sets: list[str] = []
    params: list[Any] = []
    for key, value in patch.items():
        if value is None:
            continue
        if key not in PATCHABLE_FIELDS:
            raise ValueError(f"field_not_allowed:{key}")
        col, typ = PATCHABLE_FIELDS[key]
        if typ is bool and not isinstance(value, bool):
            raise ValueError(f"bad_type:{key} must be bool")
        if typ is str and not isinstance(value, str):
            raise ValueError(f"bad_type:{key} must be string")
        if key == "tier" and value not in TIER_VALUES_ALLOWED:
            raise ValueError(f"bad_tier:{value}")
        if key == "status" and value not in STATUS_VALUES_ALLOWED:
            raise ValueError(f"bad_status:{value}")
        if key == "stage" and value not in STAGE_VALUES_ALLOWED:
            raise ValueError(f"bad_stage:{value}")
        sets.append(f"{col} = %s")
        params.append(value)
        if key == "stage":
            sets.append("stage_updated_at = now()")
    if not sets:
        raise ValueError("empty_patch_after_filter")
    params.append(lead_id)
    sql = f"UPDATE leads SET {', '.join(sets)} WHERE lead_id = %s RETURNING {_BASE_COLS}"
    with _db._conn() as c, _cursor(c) as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return flatten(dict(row)) if row else None


def delete_lead(lead_id: str) -> bool:
    with _db._conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM leads WHERE lead_id = %s", (lead_id,))
        return cur.rowcount > 0


def delete_rejects() -> int:
    with _db._conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM leads WHERE tier = 'Reject'")
        return cur.rowcount


# --- Review (Haiku) ---------------------------------------------------------

def list_review_queue(all_pending: bool = False) -> list[dict]:
    """Raw lead rows needing triage: status='New' and ai_reviewed=false.
    Default scopes to rows created today; all_pending drops the date filter."""
    sql = "SELECT * FROM leads WHERE status = 'New' AND ai_reviewed = FALSE"
    if not all_pending:
        sql += " AND created_at::date = current_date"
    sql += " ORDER BY lead_score DESC, posting_age_days DESC NULLS LAST"
    with _db._conn() as c, _cursor(c) as cur:
        cur.execute(sql)
        return [dict(r) for r in cur.fetchall()]


def update_review(lead_id: str, *, tier: str, angle: str, breakdown: str) -> bool:
    """Persist a triage verdict: tier + one-line angle + appended breakdown."""
    with _db._conn() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE leads SET tier = %s, angle = %s, breakdown = %s, "
            "ai_reviewed = TRUE WHERE lead_id = %s",
            (tier, angle, breakdown, lead_id),
        )
        return cur.rowcount > 0


# --- Outreach (Sonnet) ------------------------------------------------------

def list_outreach_queue() -> list[dict]:
    """Raw rows the recruiter flagged for outreach that have not been drafted."""
    with _db._conn() as c, _cursor(c) as cur:
        cur.execute("SELECT * FROM leads WHERE build_outreach = TRUE "
                    "AND outreach_created = FALSE "
                    "ORDER BY lead_score DESC, posting_age_days DESC NULLS LAST")
        return [dict(r) for r in cur.fetchall()]


def save_outreach(lead_id: str, *, linkedin: str, email_subject: str,
                  email_body: str, package_path: str = "") -> bool:
    """Store the drafted LinkedIn + email, mark outreach_created, clear the gate."""
    with _db._conn() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE leads SET outreach_linkedin = %s, outreach_email_subject = %s, "
            "outreach_email_body = %s, package_path = %s, outreach_created = TRUE, "
            "build_outreach = FALSE WHERE lead_id = %s",
            (linkedin, email_subject, email_body, package_path, lead_id),
        )
        return cur.rowcount > 0


def list_corrections(limit: int = 20) -> list[dict]:
    """Recent rows where the human overrode an AI REJECT, for few-shot learning."""
    with _db._conn() as c, _cursor(c) as cur:
        cur.execute("SELECT * FROM leads WHERE ai_override = TRUE "
                    "ORDER BY created_at DESC LIMIT %s", (limit,))
        return [dict(r) for r in cur.fetchall()]


def stats() -> dict[str, int]:
    sql = """
        SELECT
          count(*) FILTER (WHERE created_at > now() - interval '7 days')             AS new_last_7d,
          count(*) FILTER (WHERE tier = '')                                          AS to_triage,
          count(*) FILTER (WHERE build_outreach AND NOT outreach_created)            AS queued_for_outreach,
          count(*) FILTER (WHERE outreach_created AND status IN ('New', ''))         AS drafted_awaiting_send,
          count(*) FILTER (WHERE status = 'Contacted'
                           AND updated_at > now() - interval '30 days')              AS contacted_last_30d
        FROM leads;
    """
    with _db._conn() as c, _cursor(c) as cur:
        cur.execute(sql)
        return {k: int(v) for k, v in dict(cur.fetchone()).items()}


if __name__ == "__main__":
    print("stats:", stats())
    for r in list_leads(limit=3):
        print("  ", r["lead_id"], r["role_title"][:36], "|", r["company"][:20],
              "| age", r["posting_age_days"], "| score", r["score"])
