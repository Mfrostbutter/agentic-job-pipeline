"""Recruiter-mode endpoint logic: orchestrates review_leads.py + outreach_packages.py.

Mirrors jobs_api.py. The pipeline scripts live in /app/pipeline and are invoked
as subprocesses with --json so this service stays decoupled from their internals
and the scripts remain usable as standalone CLI tools.

Review (Haiku) is the cheap automatic leg the n8n workflow triggers. Outreach
drafting (Sonnet) is the expensive leg, gated behind the manual per-lead
build_outreach flag, and funnels through a single lock so overlapping triggers
never double-bill the API.
"""
from __future__ import annotations

import json
import logging
import subprocess
import threading
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path as FPath, Query
from pydantic import BaseModel, Field

# Import config first: it inserts PIPELINE_PATH into sys.path, which makes the
# pipeline package (leads_repo) importable below.
from .config import PIPELINE_PATH, TZ

import leads_repo  # noqa: E402, resolvable only after config sets sys.path

log = logging.getLogger("jobpipeline.leads")

REVIEW_SCRIPT = PIPELINE_PATH / "review_leads.py"
OUTREACH_SCRIPT = PIPELINE_PATH / "outreach_packages.py"

# Single lock across /leads/outreach and /leads/review?chain_outreach=true.
_OUTREACH_LOCK = threading.Lock()


class OutreachInProgress(Exception):
    """Raised when run_outreach() is invoked while another draft run is active."""


def outreach_in_progress() -> bool:
    return _OUTREACH_LOCK.locked()


def _run_pipeline_script(script_path, extra_args: list[str], timeout: int) -> dict[str, Any]:
    cmd = ["python", str(script_path), "--json", *extra_args]
    log.info("Running: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            cwd=str(PIPELINE_PATH),
        )
    except subprocess.TimeoutExpired:
        log.error("Pipeline script timed out after %ds: %s", timeout, script_path.name)
        return {"error": "timeout", "script": script_path.name}

    if result.returncode != 0:
        log.error("Pipeline script failed (rc=%d): stderr=%s",
                  result.returncode, result.stderr[:1000])
        return {"error": "nonzero_exit", "rc": result.returncode,
                "stderr": result.stderr[:2000], "script": script_path.name}

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        log.error("Could not parse pipeline JSON: %s  stdout=%s", e, result.stdout[:1000])
        return {"error": "bad_json", "stdout": result.stdout[:2000],
                "stderr": result.stderr[:2000], "script": script_path.name}


def run_review(*, all_pending: bool = False, limit: int | None = None) -> dict[str, Any]:
    extra: list[str] = []
    if all_pending:
        extra.append("--all-pending")
    if limit is not None:
        extra.extend(["--limit", str(limit)])
    return _run_pipeline_script(REVIEW_SCRIPT, extra, timeout=600)


def run_outreach(*, limit: int | None = None) -> dict[str, Any]:
    extra: list[str] = []
    if limit is not None:
        extra.extend(["--limit", str(limit)])

    acquired = _OUTREACH_LOCK.acquire(blocking=False)
    if not acquired:
        log.info("outreach skipped, already in flight")
        raise OutreachInProgress("An outreach run is already active; skipped this trigger")
    try:
        # Slow leg: ~15-30s per lead. 55 min ceiling fits an n8n 60-min trigger.
        return _run_pipeline_script(OUTREACH_SCRIPT, extra, timeout=3300)
    finally:
        _OUTREACH_LOCK.release()


def run_review_then_outreach(*, chain_outreach: bool, all_pending: bool = False,
                             limit: int | None = None) -> dict[str, Any]:
    today = datetime.now().strftime("%Y-%m-%d")
    review_result = run_review(all_pending=all_pending, limit=limit)

    outreach_result: dict[str, Any] | None = None
    if chain_outreach and not review_result.get("error"):
        try:
            outreach_result = run_outreach()
        except OutreachInProgress:
            outreach_result = {"eligible": 0, "drafted": 0, "failed": 0, "leads": [],
                               "failures": [], "note": "Outreach already in progress; skipped."}

    return {"date": today, "tz": TZ, "review": review_result, "outreach": outreach_result}


# ===========================================================================
# Leads UI endpoints (/leads/api/*) - bearer-gated at the router include site.
# ===========================================================================

class LeadPatch(BaseModel):
    tier:            str | None  = Field(None)
    build_outreach:  bool | None = Field(None)
    status:          str | None  = Field(None)
    ai_reviewed:     bool | None = Field(None)
    ai_override:     bool | None = Field(None)
    override_reason: str | None  = Field(None, max_length=500)
    stage:           str | None  = Field(None)
    contact_name:    str | None  = Field(None, max_length=200)
    contact_handle:  str | None  = Field(None, max_length=200)


router = APIRouter(prefix="/leads/api", tags=["leads-ui"])


def _require_lead_id(record_id: str = FPath(..., min_length=1, max_length=128)) -> str:
    lid = record_id.strip()
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:_-")
    if not lid or not set(lid) <= allowed:
        raise HTTPException(status_code=400,
                            detail={"error": "bad_lead_id", "detail": "Invalid lead_id"})
    return lid


@router.get("/leads")
def list_leads(
    tier:     str = Query("all", description="Top Tier | Second Choice | Reject | all"),
    status_:  str = Query("all", alias="status"),
    campaign: str = Query("all"),
    limit:    int = Query(200, ge=1, le=1000),
) -> list[dict]:
    return leads_repo.list_leads(tier=tier, status=status_, campaign=campaign, limit=limit)


@router.get("/stats")
def leads_stats() -> dict:
    try:
        return leads_repo.stats()
    except Exception as e:  # noqa: BLE001
        log.exception("leads stats failed")
        raise HTTPException(status_code=500, detail={"error": "stats_failed", "detail": str(e)[:200]})


@router.get("/leads/{record_id}")
def get_lead(record_id: str = Depends(_require_lead_id)) -> dict:
    """Full lead detail including description, breakdown, and drafted outreach."""
    lead = leads_repo.get_lead(record_id)
    if lead is None:
        raise HTTPException(status_code=404,
                            detail={"error": "not_found", "detail": f"lead {record_id} not found"})
    return lead


@router.patch("/leads/{record_id}")
def patch_lead(body: LeadPatch, record_id: str = Depends(_require_lead_id)) -> dict:
    data = body.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(status_code=400, detail={"error": "empty_patch", "detail": "Provide at least one field"})
    try:
        updated = leads_repo.patch_lead(record_id, data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail={"error": "bad_patch", "detail": str(e)})
    if updated is None:
        raise HTTPException(status_code=404, detail={"error": "not_found", "detail": f"lead {record_id} not found"})
    return updated


@router.delete("/leads/{record_id}")
def delete_lead(record_id: str = Depends(_require_lead_id)) -> dict:
    if not leads_repo.delete_lead(record_id):
        raise HTTPException(status_code=404, detail={"error": "not_found", "record_id": record_id})
    log.info("deleted lead lead_id=%s", record_id)
    return {"deleted": True, "record_id": record_id}


@router.post("/leads/purge-rejects")
def purge_rejects(dry_run: bool = Query(False)) -> dict:
    if dry_run:
        rows = [r for r in leads_repo.list_leads(tier="Reject", limit=1000)]
        return {"would_delete": len(rows), "dry_run": True}
    deleted = leads_repo.delete_rejects()
    log.info("leads purge_rejects: deleted=%d", deleted)
    return {"deleted": deleted}
