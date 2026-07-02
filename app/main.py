"""Job Pipeline - FastAPI service hosting the review/build API + triage UI.

The service orchestrates two Claude agent scripts (pipeline/review_new_jobs.py
and pipeline/build_packages.py) as subprocesses and serves a single-file SPA
for human triage. All state lives in Postgres (see schema.sql).
"""

from __future__ import annotations

import logging
import sys
from typing import Annotated

from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import jobs_api, leads_api, slack
from .config import API_BEARER_TOKEN, VERSION

STATIC_DIR = Path(__file__).resolve().parent / "static"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("jobpipeline")

app = FastAPI(title="job-pipeline", version=VERSION)


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

def require_bearer(authorization: Annotated[str | None, Header()] = None) -> None:
    if not API_BEARER_TOKEN:
        # Service was started without a token configured. Refuse rather than
        # silently allow everything.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API_BEARER_TOKEN not configured; service is locked down.",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Missing bearer token")
    presented = authorization[len("Bearer "):].strip()
    if presented != API_BEARER_TOKEN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Invalid token")


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class ReviewRequest(BaseModel):
    chain_build: bool = Field(False, description="If true, run build_packages.py after review when Top Tier records emerge. Default false: builds are gated on the manual Build Package flag so you control API spend.")
    all_pending: bool = Field(False, description="Drop the today-only filter; review every unreviewed Status=New record.")
    limit: int | None = Field(None, description="Cap review to N records (smoke tests).")
    no_pdf: bool = Field(False, description="Skip PDF rendering in build phase.")
    post_slack: bool = Field(False, description="Post the consolidated summary to Slack from within the service. If false, the caller (n8n) renders the Slack message itself.")


class BuildRequest(BaseModel):
    limit: int | None = Field(None)
    no_pdf: bool = Field(False)


class LeadReviewRequest(BaseModel):
    chain_outreach: bool = Field(False, description="If true, draft outreach after review for any leads already flagged build_outreach. Default false: drafting is gated on the manual flag so you control API spend.")
    all_pending: bool = Field(False, description="Drop the today-only filter; review every unreviewed Status=New lead.")
    limit: int | None = Field(None, description="Cap review to N leads (smoke tests).")
    post_slack: bool = Field(False, description="Post the summary to Slack from within the service. If false, the caller (n8n) renders the Slack message itself.")


class OutreachRequest(BaseModel):
    limit: int | None = Field(None)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "job-pipeline", "version": VERSION}


@app.post("/jobs/review", dependencies=[Depends(require_bearer)])
def jobs_review(req: ReviewRequest) -> dict:
    log.info("POST /jobs/review chain_build=%s all_pending=%s limit=%s",
             req.chain_build, req.all_pending, req.limit)
    result = jobs_api.run_review_then_build(
        chain_build=req.chain_build,
        all_pending=req.all_pending,
        limit=req.limit,
        no_pdf=req.no_pdf,
    )
    if req.post_slack:
        slack.post_review_summary(result)
    return result


@app.post("/jobs/build", dependencies=[Depends(require_bearer)])
def jobs_build(req: BuildRequest) -> dict:
    log.info("POST /jobs/build limit=%s no_pdf=%s", req.limit, req.no_pdf)
    try:
        return jobs_api.run_build(limit=req.limit, no_pdf=req.no_pdf)
    except jobs_api.BuildInProgress as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "build_in_progress",
                "message": str(e) or "A build is already running; skipped this trigger",
            },
        )


@app.get("/jobs/build/status", dependencies=[Depends(require_bearer)])
def jobs_build_status() -> dict:
    return {"running": jobs_api.build_in_progress()}


# ---------------------------------------------------------------------------
# Recruiter mode ("aging listings"): review = Haiku triage (auto, in-workflow);
# outreach = Sonnet LinkedIn + email drafting (manual, gated on build_outreach).
# ---------------------------------------------------------------------------

@app.post("/leads/review", dependencies=[Depends(require_bearer)])
def leads_review(req: LeadReviewRequest) -> dict:
    log.info("POST /leads/review chain_outreach=%s all_pending=%s limit=%s",
             req.chain_outreach, req.all_pending, req.limit)
    result = leads_api.run_review_then_outreach(
        chain_outreach=req.chain_outreach,
        all_pending=req.all_pending,
        limit=req.limit,
    )
    if req.post_slack:
        slack.post_lead_summary(result)
    return result


@app.post("/leads/outreach", dependencies=[Depends(require_bearer)])
def leads_outreach(req: OutreachRequest) -> dict:
    log.info("POST /leads/outreach limit=%s", req.limit)
    try:
        return leads_api.run_outreach(limit=req.limit)
    except leads_api.OutreachInProgress as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "outreach_in_progress",
                    "message": str(e) or "An outreach run is already active; skipped this trigger"},
        )


@app.get("/leads/outreach/status", dependencies=[Depends(require_bearer)])
def leads_outreach_status() -> dict:
    return {"running": leads_api.outreach_in_progress()}


# ---------------------------------------------------------------------------
# Jobs UI
# ---------------------------------------------------------------------------
# All /jobs/api/* endpoints are defined on jobs_api.router and are bearer-gated
# at the router level so n8n callers and the SPA both go through one auth path.

app.include_router(jobs_api.router, dependencies=[Depends(require_bearer)])


@app.get("/jobs/ui", include_in_schema=False)
def jobs_ui() -> FileResponse:
    """Serve the single-file SPA. Token gating is done client-side: the page
    loads without auth (so the user can be prompted for the bearer token),
    but every /jobs/api/* call it makes is bearer-checked server-side."""
    html_path = STATIC_DIR / "jobs_ui.html"
    if not html_path.is_file():
        raise HTTPException(status_code=500, detail="jobs_ui.html not found")
    return FileResponse(str(html_path), media_type="text/html")
