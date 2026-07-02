"""
seen_jobs_db.py, Postgres helper for the job pipeline dedup layer.

Connection settings come from PG_* env vars (or pipeline/.env). Defaults match
docker-compose.yml, where the database runs as the `postgres` service.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Optional

import psycopg2
from dotenv import load_dotenv

_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(_ENV_PATH)

_CONN_PARAMS = {
    "host": os.environ.get("PG_HOST", "localhost"),
    "port": int(os.environ.get("PG_PORT", 5432)),
    "dbname": os.environ.get("PG_DB", "jobpipeline"),
    "user": os.environ.get("PG_USER", "pipeline"),
    "password": os.environ.get("PG_PASSWORD", ""),
    "connect_timeout": 5,
    "client_encoding": "UTF8",
}


@contextmanager
def _conn():
    c = psycopg2.connect(**_CONN_PARAMS)
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def is_seen(job_id: str) -> bool:
    """Return True if job_id already exists in seen_jobs."""
    if not job_id:
        return False
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT 1 FROM seen_jobs WHERE job_id = %s LIMIT 1;", (job_id,))
        return cur.fetchone() is not None


def filter_unseen(job_ids: list[str]) -> set[str]:
    """Given a list of job_ids, return the subset NOT already in seen_jobs."""
    if not job_ids:
        return set()
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT job_id FROM seen_jobs WHERE job_id = ANY(%s::text[]);",
            (list(job_ids),),
        )
        seen = {row[0] for row in cur.fetchall()}
    return {j for j in job_ids if j not in seen}


def mark_seen(job_id: str, source: str, status: str = "new") -> None:
    """INSERT ... ON CONFLICT update for a job we've just recorded."""
    if not job_id:
        return
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO seen_jobs (job_id, source, status)
            VALUES (%s, %s, %s)
            ON CONFLICT (job_id) DO UPDATE SET last_updated = NOW();
            """,
            (job_id, (source or "").lower(), status),
        )


def mark_status_by_job_id(job_id: str, status: str, reason: Optional[str] = None) -> int:
    """UPDATE seen_jobs.status by job_id. Returns rows affected.

    Manually-added jobs may have no seen_jobs row; those simply affect 0 rows,
    which is fine."""
    if not job_id:
        return 0
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """
            UPDATE seen_jobs
            SET status = %s, reason = COALESCE(%s, reason), last_updated = NOW()
            WHERE job_id = %s;
            """,
            (status, reason, job_id),
        )
        return cur.rowcount


if __name__ == "__main__":
    # Quick connectivity test
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT COUNT(*), MAX(last_updated) FROM seen_jobs;")
        n, last = cur.fetchone()
        print(f"seen_jobs: {n} rows, last_updated={last}")
