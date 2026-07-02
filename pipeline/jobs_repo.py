"""jobs_repo.py - Postgres data-access layer for the job pipeline.

System of record for job rows. Returns a flat-dict shape the web UI consumes.
Reuses seen_jobs_db's connection settings.

The dict carries both `job_id` and `record_id` (same value); the frontend is
keyed on `record_id`.
"""
from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal
from typing import Any, Optional

import psycopg2.extras

import seen_jobs_db as _db

# Columns we read for the flat shape. description/breakdown pulled only on detail.
_BASE_COLS = (
    "job_id, title, company, location, source_url, workplace, status, "
    "relevance_score, salary_min, salary_max, tier, build_package, "
    "application_package_created, ai_reviewed, ai_override, override_reason, "
    "package_path, breakdown, stage, stage_updated_at, created_at"
)

# PATCH body field -> (column, python type). Mirrors the old PATCHABLE_FIELDS.
PATCHABLE_FIELDS: dict[str, tuple[str, type]] = {
    "tier":            ("tier", str),
    "build_package":   ("build_package", bool),
    "status":          ("status", str),
    "ai_reviewed":     ("ai_reviewed", bool),
    "ai_override":     ("ai_override", bool),
    "override_reason": ("override_reason", str),
    "stage":           ("stage", str),
}

TIER_VALUES_ALLOWED = {"Top Tier", "Second Choice", "Reject", ""}
STATUS_VALUES_ALLOWED = {"New", "Applied", "Built", "Archived", ""}
STAGE_VALUES_ALLOWED = {"", "Applied", "Heard Back", "Screen", "Interview",
                        "Offer", "Rejected", "Ghosted"}


def _num(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, Decimal):
        f = float(v)
        return int(f) if f.is_integer() else f
    return v


def flatten(row: dict) -> dict:
    """Translate a jobs row into the flat dict the UI consumes."""
    return {
        "record_id": row["job_id"],   # transition alias
        "job_id":    row["job_id"],
        "created_at": row["created_at"],
        "title":      row["title"] or "",
        "company":    row["company"] or "",
        "location":   row["location"] or "",
        "workplace":  row["workplace"] or "",
        "url":        row["source_url"] or "",
        "tier":       row["tier"] or "",
        "status":     row["status"] or "",
        "score":      _num(row["relevance_score"]),
        "sal_min":    _num(row["salary_min"]),
        "sal_max":    _num(row["salary_max"]),
        "build_package":               bool(row["build_package"]),
        "application_package_created": bool(row["application_package_created"]),
        "ai_reviewed":                 bool(row["ai_reviewed"]),
        "ai_override":                 bool(row["ai_override"]),
        "override_reason":             row["override_reason"] or "",
        "was_ai_rejected":             "] REJECT:" in (row["breakdown"] or ""),
        "package_path":                row["package_path"] or "",
        "stage":                       row["stage"] or "",
        "stage_updated_at":            row["stage_updated_at"],
    }


def _cursor(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def list_jobs(*, tier: str = "all", status: str = "all", limit: int = 100) -> list[dict]:
    clauses: list[str] = []
    params: list[Any] = []
    if tier and tier.lower() != "all":
        clauses.append("tier = %s")
        params.append(tier)
    if status and status.lower() != "all":
        s = status.lower()
        if s == "built":
            clauses.append("application_package_created = TRUE")
        elif s == "new":
            # "New" = recently scraped, not the sticky status='New' flag (which
            # never changes and made this filter a no-op). Last 24h by created_at.
            clauses.append("created_at >= now() - interval '24 hours'")
        elif s == "applied":
            clauses.append("status = 'Applied'")
        else:
            clauses.append("status = %s")
            params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = (f"SELECT {_BASE_COLS} FROM jobs {where} "
           f"ORDER BY created_at DESC LIMIT %s")
    params.append(limit)
    with _db._conn() as c, _cursor(c) as cur:
        cur.execute(sql, params)
        return [flatten(dict(r)) for r in cur.fetchall()]


_CREATE_COLS = ["job_id", "title", "company", "location", "source_url", "workplace",
                "status", "tier", "stage", "description", "salary_min", "salary_max",
                "relevance_score", "ai_reviewed", "breakdown"]


def create_job(data: dict) -> tuple[dict, bool]:
    """Insert a manually-added job (off-platform applications). Returns
    (flat_row, created). job_id is derived from the source URL when present so
    re-adding the same posting is idempotent, else a random 'manual:' id. The
    synthetic id never collides with LinkedIn numeric ids, so the scraper won't
    duplicate it. Also seeds seen_jobs for ledger consistency."""
    title = (data.get("title") or "").strip()
    company = (data.get("company") or "").strip()
    if not title or not company:
        raise ValueError("title and company are required")

    url = (data.get("source_url") or "").strip()
    if url:
        job_id = "manual:" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    else:
        job_id = "manual:" + uuid.uuid4().hex[:16]

    tier = data.get("tier") or ""
    status = data.get("status") or "New"
    stage = data.get("stage") or ""
    if tier not in TIER_VALUES_ALLOWED:
        raise ValueError(f"bad_tier:{tier}")
    if status not in STATUS_VALUES_ALLOWED:
        raise ValueError(f"bad_status:{status}")
    if stage not in STAGE_VALUES_ALLOWED:
        raise ValueError(f"bad_stage:{stage}")

    row = {
        "job_id": job_id, "title": title, "company": company,
        "location": (data.get("location") or "").strip(),
        "source_url": url, "workplace": (data.get("workplace") or "").strip(),
        "status": status, "tier": tier, "stage": stage,
        "description": data.get("description") or "",
        "salary_min": data.get("salary_min"), "salary_max": data.get("salary_max"),
        "relevance_score": data.get("relevance_score"),
        "ai_reviewed": True, "breakdown": "[manual] Added via UI",
    }
    placeholders = ", ".join(["%s"] * len(_CREATE_COLS))
    sql = (f"INSERT INTO jobs ({', '.join(_CREATE_COLS)}) VALUES ({placeholders}) "
           f"ON CONFLICT (job_id) DO NOTHING RETURNING {_BASE_COLS}")
    with _db._conn() as c, _cursor(c) as cur:
        cur.execute(sql, [row[col] for col in _CREATE_COLS])
        r = cur.fetchone()
        created = r is not None
        if created:
            if stage:
                cur.execute("UPDATE jobs SET stage_updated_at = now() WHERE job_id = %s", (job_id,))
            cur.execute("INSERT INTO seen_jobs (job_id, source, status) VALUES (%s, 'manual', 'new') "
                        "ON CONFLICT (job_id) DO NOTHING", (job_id,))
        cur.execute(f"SELECT {_BASE_COLS} FROM jobs WHERE job_id = %s", (job_id,))
        r = cur.fetchone()
    return flatten(dict(r)), created


def get_job(job_id: str) -> Optional[dict]:
    """Full detail incl. description + breakdown, or None if not found."""
    sql = f"SELECT {_BASE_COLS}, description FROM jobs WHERE job_id = %s"
    with _db._conn() as c, _cursor(c) as cur:
        cur.execute(sql, (job_id,))
        row = cur.fetchone()
    if not row:
        return None
    base = flatten(dict(row))
    base["description"] = row["description"] or ""
    base["breakdown"]   = row["breakdown"] or ""
    return base


def get_raw(job_id: str) -> Optional[dict]:
    """Return all columns as a plain dict (for build/package-path resolution)."""
    with _db._conn() as c, _cursor(c) as cur:
        cur.execute("SELECT * FROM jobs WHERE job_id = %s", (job_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def patch_job(job_id: str, patch: dict[str, Any]) -> Optional[dict]:
    """Update whitelisted columns. Returns the flattened updated row, or None
    if the row does not exist. Raises ValueError on validation failure."""
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
    params.append(job_id)
    sql = (f"UPDATE jobs SET {', '.join(sets)} WHERE job_id = %s "
           f"RETURNING {_BASE_COLS}")
    with _db._conn() as c, _cursor(c) as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return flatten(dict(row)) if row else None


def set_package_path(job_id: str, package_path: str) -> bool:
    with _db._conn() as c, c.cursor() as cur:
        cur.execute("UPDATE jobs SET package_path = %s WHERE job_id = %s",
                    (package_path, job_id))
        return cur.rowcount > 0


def delete_job(job_id: str) -> bool:
    with _db._conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM jobs WHERE job_id = %s", (job_id,))
        return cur.rowcount > 0


def list_reject_ids() -> list[str]:
    with _db._conn() as c, c.cursor() as cur:
        cur.execute("SELECT job_id FROM jobs WHERE tier = 'Reject'")
        return [r[0] for r in cur.fetchall()]


def delete_rejects() -> int:
    with _db._conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM jobs WHERE tier = 'Reject'")
        return cur.rowcount


def list_eligible_for_build() -> list[dict]:
    """Raw rows where build_package is set and no package built yet.
    Tier is informational; the gate is the manual build_package flag."""
    with _db._conn() as c, _cursor(c) as cur:
        cur.execute("SELECT * FROM jobs WHERE build_package = TRUE "
                    "AND application_package_created = FALSE "
                    "ORDER BY created_at DESC")
        return [dict(r) for r in cur.fetchall()]


def list_review_queue(all_pending: bool = False) -> list[dict]:
    """Raw rows needing triage: Status='New' and ai_reviewed=false.
    Default scopes to rows created today (created_at::date = current_date);
    all_pending drops the date filter."""
    sql = ("SELECT * FROM jobs WHERE status = 'New' AND ai_reviewed = FALSE")
    if not all_pending:
        sql += " AND created_at::date = current_date"
    sql += " ORDER BY created_at DESC"
    with _db._conn() as c, _cursor(c) as cur:
        cur.execute(sql)
        return [dict(r) for r in cur.fetchall()]


def list_corrections(limit: int = 20) -> list[dict]:
    """Recent rows where the human overrode an AI REJECT, for few-shot learning.
    Explicit (ai_override) first, then implicit (kept Top/Second despite a
    '] REJECT:' breakdown), deduped by job_id, newest first."""
    with _db._conn() as c, _cursor(c) as cur:
        cur.execute("SELECT * FROM jobs WHERE ai_override = TRUE "
                    "ORDER BY created_at DESC LIMIT %s", (limit,))
        explicit = [dict(r) for r in cur.fetchall()]
        if len(explicit) >= limit:
            return explicit[:limit]
        seen = {r["job_id"] for r in explicit}
        cur.execute(
            "SELECT * FROM jobs WHERE tier IN ('Top Tier', 'Second Choice') "
            "AND ai_reviewed = TRUE AND breakdown LIKE %s "
            "ORDER BY created_at DESC LIMIT %s", ("%] REJECT:%", limit))
        implicit = [dict(r) for r in cur.fetchall() if r["job_id"] not in seen]
    return (explicit + implicit)[:limit]


def update_review(job_id: str, *, tier: str, breakdown: str,
                  salary_min: Optional[int] = None,
                  salary_max: Optional[int] = None) -> bool:
    """Persist a triage verdict: tier + appended breakdown + ai_reviewed, and
    optionally extracted salaries."""
    sets = ["tier = %s", "breakdown = %s", "ai_reviewed = TRUE"]
    params: list[Any] = [tier, breakdown]
    if salary_min is not None:
        sets.append("salary_min = %s")
        params.append(salary_min)
    if salary_max is not None:
        sets.append("salary_max = %s")
        params.append(salary_max)
    params.append(job_id)
    with _db._conn() as c, c.cursor() as cur:
        cur.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE job_id = %s", params)
        return cur.rowcount > 0


def mark_built(job_id: str, package_path: str) -> bool:
    """Stamp a row as built: application_package_created=true, store the path,
    and clear build_package so it leaves the build queue."""
    with _db._conn() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET application_package_created = TRUE, "
            "package_path = %s, build_package = FALSE WHERE job_id = %s",
            (package_path, job_id),
        )
        return cur.rowcount > 0


def stats() -> dict[str, int]:
    sql = """
        SELECT
          count(*) FILTER (WHERE created_at > now() - interval '7 days')        AS new_last_7d,
          count(*) FILTER (WHERE tier = '')                                     AS to_triage,
          count(*) FILTER (WHERE build_package AND NOT application_package_created) AS queued_to_build,
          count(*) FILTER (WHERE application_package_created
                           AND status IN ('New', ''))                          AS built_awaiting_review,
          count(*) FILTER (WHERE status = 'Applied'
                           AND updated_at > now() - interval '30 days')         AS applied_last_30d
        FROM jobs;
    """
    with _db._conn() as c, _cursor(c) as cur:
        cur.execute(sql)
        return {k: int(v) for k, v in dict(cur.fetchone()).items()}


if __name__ == "__main__":
    print("stats:", stats())
    rows = list_jobs(tier="Top Tier", limit=3)
    print(f"top-tier sample ({len(rows)}):")
    for r in rows:
        print("  ", r["job_id"], r["title"][:40], "|", r["company"][:20])
