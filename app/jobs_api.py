"""Job pipeline endpoint logic: orchestrates review_new_jobs.py + build_packages.py.

The pipeline scripts live in /app/pipeline. We invoke them as subprocesses with
--json so this service stays decoupled from their internals; the scripts remain
usable as standalone CLI tools.

Data layer: jobs live in Postgres (`jobs` table) via the shared `jobs_repo`
module. Records are keyed by the natural `job_id`. The flat dicts also carry a
`record_id` alias (== job_id) that the UI is keyed on.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path as FPath, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

# Import config first: it inserts PIPELINE_PATH into sys.path, which makes the
# pipeline package (jobs_repo, seen_jobs_db, build_packages) importable below.
from .config import APPLICATIONS_DIR, PIPELINE_PATH, TZ

import jobs_repo  # noqa: E402, resolvable only after config sets sys.path

log = logging.getLogger("jobpipeline.jobs")


REVIEW_SCRIPT = PIPELINE_PATH / "review_new_jobs.py"
BUILD_SCRIPT  = PIPELINE_PATH / "build_packages.py"
RENDER_SCRIPT = PIPELINE_PATH / "render_resume.py"


# Singleton concurrency lock for build_packages.py invocations.
# FastAPI endpoints here are sync (def, not async def) so they execute in the
# threadpool; threading.Lock is the correct primitive (not asyncio.Lock).
# Covers both /jobs/build and /jobs/review?chain_build=true entry points
# because both funnel through run_build().
_BUILD_LOCK = threading.Lock()


class BuildInProgress(Exception):
    """Raised when run_build() is invoked while another build is already running."""


def build_in_progress() -> bool:
    """Return True if a build is currently holding the lock."""
    return _BUILD_LOCK.locked()


def _run_pipeline_script(script_path, extra_args: list[str], timeout: int) -> dict[str, Any]:
    cmd = ["python", str(script_path), "--json", *extra_args]
    log.info("Running: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(PIPELINE_PATH),
        )
    except subprocess.TimeoutExpired:
        log.error("Pipeline script timed out after %ds: %s", timeout, script_path.name)
        return {"error": "timeout", "script": script_path.name}

    if result.returncode != 0:
        log.error("Pipeline script failed (rc=%d): stderr=%s",
                  result.returncode, result.stderr[:1000])
        return {
            "error": "nonzero_exit",
            "rc": result.returncode,
            "stderr": result.stderr[:2000],
            "script": script_path.name,
        }

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        log.error("Could not parse pipeline JSON: %s  stdout=%s", e, result.stdout[:1000])
        return {
            "error": "bad_json",
            "stdout": result.stdout[:2000],
            "stderr": result.stderr[:2000],
            "script": script_path.name,
        }


def run_review(*, all_pending: bool = False, limit: int | None = None) -> dict[str, Any]:
    extra: list[str] = []
    if all_pending:
        extra.append("--all-pending")
    if limit is not None:
        extra.extend(["--limit", str(limit)])
    # Review is usually fast: 50 jobs * ~3s = ~2.5 min. Allow 10 min headroom.
    return _run_pipeline_script(REVIEW_SCRIPT, extra, timeout=600)


def run_build(*, limit: int | None = None, no_pdf: bool = False) -> dict[str, Any]:
    extra: list[str] = []
    if limit is not None:
        extra.extend(["--limit", str(limit)])
    if no_pdf:
        extra.append("--no-pdf")

    # Non-blocking acquire: if a build is already running, raise so the caller
    # (FastAPI endpoint) can translate to HTTP 409. This protects against
    # overlapping cron + manual triggers double-billing the Anthropic API.
    acquired = _BUILD_LOCK.acquire(blocking=False)
    if not acquired:
        log.info("build skipped, already in flight")
        raise BuildInProgress("A build is already running; skipped this trigger")

    try:
        # Build is the slow leg: ~30-60s per top job. Allow 55 min ceiling for batches
        # (fits inside an n8n trigger 60-min timeout with a small margin).
        return _run_pipeline_script(BUILD_SCRIPT, extra, timeout=3300)
    finally:
        _BUILD_LOCK.release()


def run_review_then_build(*, chain_build: bool, all_pending: bool = False,
                          limit: int | None = None, no_pdf: bool = False) -> dict[str, Any]:
    today = datetime.now().strftime("%Y-%m-%d")
    review_result = run_review(all_pending=all_pending, limit=limit)

    build_result: dict[str, Any] | None = None
    if chain_build and not review_result.get("error"):
        top_count = review_result.get("top", 0)
        if top_count > 0:
            try:
                build_result = run_build(no_pdf=no_pdf)
            except BuildInProgress:
                log.info("chain_build skipped: another build is already in flight")
                build_result = {"eligible": 0, "built": 0, "failed": 0, "packages": [], "failures": [],
                                "note": "Build already in progress; chain_build skipped this trigger."}
        else:
            build_result = {"eligible": 0, "built": 0, "failed": 0, "packages": [], "failures": [],
                            "note": "No new Top Tier hits this run; build skipped."}

    return {
        "date": today,
        "tz": TZ,
        "review": review_result,
        "build": build_result,
    }


# ===========================================================================
# Jobs UI endpoints
# ===========================================================================

# Whitelist of writable markdown filenames inside a package directory.
# Anything outside this set is rejected with 400 to prevent path traversal /
# accidental overwrite of resume.pdf, notes from other tooling, etc.
PACKAGE_FILE_WHITELIST = {
    "resume.md",
    "cover-letter.md",
    "email-body.md",
    "linkedin-outreach.md",
    "notes.md",
}


# ---------------------------------------------------------------------------
# Package-directory resolution (filesystem)
# ---------------------------------------------------------------------------

def _resolve_package_dir(package_path: str) -> Path | None:
    """package_path is typically an absolute path written by build_packages.py.
    We anchor on the directory name under APPLICATIONS_DIR so the value stays
    valid across host/container path differences.

    Returns None if we can't locate a valid directory under APPLICATIONS_DIR."""
    if not package_path:
        return None
    raw = package_path.replace("\\", "/").rstrip("/")
    dirname = raw.rsplit("/", 1)[-1]
    if not dirname or ".." in dirname:
        return None
    candidate = APPLICATIONS_DIR / dirname
    try:
        candidate.resolve().relative_to(APPLICATIONS_DIR.resolve())
    except ValueError:
        return None
    if not candidate.is_dir():
        return None
    return candidate


def _slugify_via_build_packages(s: str) -> str:
    """Reuse build_packages._slugify so naming stays bit-identical with the build
    pipeline. Import lazily so module import doesn't pin the pipeline path."""
    import build_packages as bp  # noqa: WPS433
    return bp._slugify(s)


def _resolve_or_glob_package_dir(row: dict) -> tuple[Path | None, str | None]:
    """Resolve a package directory from a jobs row dict.

    Returns (pkg_dir, attempted_glob_or_None). When package_path is empty but a
    glob on company+title yields a unique match, the caller backfills package_path.
    """
    package_path = (row.get("package_path") or "").strip()
    if package_path:
        return (_resolve_package_dir(package_path), None)

    title   = (row.get("title") or "").strip()
    company = (row.get("company") or "").strip()
    if not title or not company:
        return (None, None)

    company_slug = _slugify_via_build_packages(company)
    role_slug    = _slugify_via_build_packages(title)
    if not company_slug or not role_slug:
        return (None, None)

    pattern = f"*-{company_slug}-{role_slug}"
    matches = sorted(p for p in APPLICATIONS_DIR.glob(pattern) if p.is_dir())
    if len(matches) == 1:
        return (matches[0], pattern)
    return (None, pattern)


def _read_md_if_exists(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        log.warning("Could not read %s: %s", path, e)
        return None


def _pdf_stale(pkg_dir: Path) -> tuple[bool, bool]:
    """Return (has_pdf, stale). Stale = resume.md mtime > resume.pdf mtime."""
    resume_md = pkg_dir / "resume.md"
    if not resume_md.is_file():
        return (False, False)
    pdf_path = pkg_dir / "resume.pdf"
    if not pdf_path.is_file():
        return (False, True)
    return (True, resume_md.stat().st_mtime > pdf_path.stat().st_mtime)


def _resume_pdf_path(pkg_dir: Path) -> Path | None:
    p = pkg_dir / "resume.pdf"
    return p if p.is_file() else None


def _get_row_or_404(job_id: str) -> dict:
    row = jobs_repo.get_raw(job_id)
    if row is None:
        raise HTTPException(status_code=404,
                            detail={"error": "not_found", "detail": f"job {job_id} not found"})
    return row


# ---------------------------------------------------------------------------
# Pydantic models for the UI endpoints
# ---------------------------------------------------------------------------

class PackageFileWrite(BaseModel):
    content: str = Field(..., description="Full markdown content to write.")


class JobCreate(BaseModel):
    title:       str
    company:     str
    source_url:  str | None = Field(None)
    location:    str | None = Field(None)
    workplace:   str | None = Field(None)
    description: str | None = Field(None)
    tier:        str | None = Field(None)
    status:      str | None = Field(None)
    stage:       str | None = Field(None)
    salary_min:  int | None = Field(None)
    salary_max:  int | None = Field(None)
    auto_build:  bool = Field(False, description="Manual-add path: mark Top Tier and immediately build a resume + cover letter package with PDFs.")


class JobPatch(BaseModel):
    tier:            str | None  = Field(None)
    build_package:   bool | None = Field(None)
    status:          str | None  = Field(None)
    ai_reviewed:     bool | None = Field(None)
    ai_override:     bool | None = Field(None)
    override_reason: str | None  = Field(None, max_length=500)
    stage:           str | None  = Field(None)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/jobs/api", tags=["jobs-ui"])


def _require_job_id(record_id: str = FPath(..., min_length=1, max_length=128)) -> str:
    """Validate the job_id path segment. job_ids are source ids (digits) or the
    synthetic 'manual:' form for hand-added jobs. Path param keeps the name
    record_id for URL/frontend compatibility; the value is a job_id."""
    jid = record_id.strip()
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:_-")
    if not jid or not set(jid) <= allowed:
        raise HTTPException(status_code=400,
                            detail={"error": "bad_job_id", "detail": "Invalid job_id"})
    return jid


@router.get("/jobs")
def list_jobs(
    tier:   str = Query("all", description="Top Tier | Second Choice | Reject | all"),
    status_: str = Query("all", alias="status", description="new | built | applied | all"),
    limit:  int = Query(100, ge=1, le=500),
) -> list[dict]:
    """List jobs filtered by tier + status."""
    return jobs_repo.list_jobs(tier=tier, status=status_, limit=limit)


@router.post("/jobs")
def create_job(body: JobCreate) -> dict:
    """Manually add a job (off-platform applications the scraper never sees).
    Idempotent on source_url. Returns the row plus a `created` flag.

    auto_build=true (the "I've vetted this, get it ready" path): mark the job
    Top Tier and synchronously build a resume + cover letter package, rendering a
    PDF of each."""
    data = body.model_dump(exclude_none=True)
    auto_build = bool(data.pop("auto_build", False))
    if auto_build and not data.get("tier"):
        data["tier"] = "Top Tier"
    try:
        row, created = jobs_repo.create_job(data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail={"error": "bad_create", "detail": str(e)})

    result = {**row, "created": created}
    if not (auto_build and created):
        return result

    rid = row["record_id"]
    jobs_repo.patch_job(rid, {"build_package": True})
    acquired = _BUILD_LOCK.acquire(blocking=False)
    if not acquired:
        result["build"] = {"built": False, "error": "build_in_progress",
                           "detail": "Another build is running; build this job from the Queue shortly."}
        return result
    try:
        import build_packages as bp  # noqa: WPS433
        try:
            result["build"] = bp.build_one_job(rid, quick=True)
        except bp.BuildGateError as e:
            result["build"] = {"built": False, "error": e.code, "detail": str(e)}
        except Exception as e:  # noqa: BLE001
            log.exception("auto_build failed")
            result["build"] = {"built": False, "error": "build_exception", "detail": str(e)[:300]}
    finally:
        _BUILD_LOCK.release()

    fresh = jobs_repo.get_job(rid)
    if fresh:
        result = {**fresh, "created": created, "build": result.get("build")}
    return result


@router.get("/stats")
def jobs_stats() -> dict:
    """Aggregate counters for the Dashboard screen."""
    try:
        return jobs_repo.stats()
    except Exception as e:  # noqa: BLE001
        log.exception("stats failed")
        raise HTTPException(status_code=500, detail={"error": "stats_failed", "detail": str(e)[:200]})


@router.get("/jobs/{record_id}")
def get_job(record_id: str = Depends(_require_job_id)) -> dict:
    """Single job detail including full description + breakdown."""
    job = jobs_repo.get_job(record_id)
    if job is None:
        raise HTTPException(status_code=404,
                            detail={"error": "not_found", "detail": f"job {record_id} not found"})
    return job


@router.get("/jobs/{record_id}/package")
def get_package(record_id: str = Depends(_require_job_id)) -> dict:
    """Read all .md files from the package directory."""
    row = _get_row_or_404(record_id)
    pkg_path = row.get("package_path") or ""
    pkg_dir, attempted_glob = _resolve_or_glob_package_dir(row)
    if pkg_dir is None:
        detail_msg = f"No package directory resolved from package_path={pkg_path!r}"
        if attempted_glob:
            detail_msg += f" (also tried glob {attempted_glob!r} under applications/, no unique match)"
        raise HTTPException(status_code=404,
                            detail={"error": "package_not_found", "detail": detail_msg})
    if attempted_glob and not pkg_path:
        jobs_repo.set_package_path(record_id, str(pkg_dir))
    has_pdf, stale = _pdf_stale(pkg_dir)
    return {
        "record_id": record_id,
        "package_dir": str(pkg_dir),
        "resume_md":       _read_md_if_exists(pkg_dir / "resume.md"),
        "cover_letter_md": _read_md_if_exists(pkg_dir / "cover-letter.md"),
        "email_md":        _read_md_if_exists(pkg_dir / "email-body.md"),
        "linkedin_md":     _read_md_if_exists(pkg_dir / "linkedin-outreach.md"),
        "notes_md":        _read_md_if_exists(pkg_dir / "notes.md"),
        "has_resume_pdf": has_pdf,
        "pdf_stale":      stale,
    }


@router.put("/jobs/{record_id}/package/{filename}")
def save_package_file(
    body: PackageFileWrite,
    record_id: str = Depends(_require_job_id),
    filename: str = FPath(..., min_length=3, max_length=64),
) -> dict:
    """Save edited markdown back to disk. Filename whitelist enforced."""
    if filename not in PACKAGE_FILE_WHITELIST:
        raise HTTPException(status_code=400,
                            detail={"error": "filename_not_allowed",
                                    "detail": f"Allowed: {sorted(PACKAGE_FILE_WHITELIST)}"})
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail={"error": "bad_filename", "detail": "Path separators not allowed"})

    row = _get_row_or_404(record_id)
    pkg_path = row.get("package_path") or ""
    pkg_dir, attempted_glob = _resolve_or_glob_package_dir(row)
    if pkg_dir is None:
        raise HTTPException(status_code=404,
                            detail={"error": "package_not_found", "detail": f"No package dir for {record_id}"})
    if attempted_glob and not pkg_path:
        jobs_repo.set_package_path(record_id, str(pkg_dir))
    target = pkg_dir / filename
    try:
        target.resolve().relative_to(pkg_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail={"error": "path_escape", "detail": "Resolved path escaped package dir"})
    try:
        normalized = body.content.replace("\r\n", "\n").replace("\r", "\n")
        if not normalized.endswith("\n"):
            normalized += "\n"
        target.write_text(normalized, encoding="utf-8")
    except OSError as e:
        raise HTTPException(status_code=500, detail={"error": "write_failed", "detail": str(e)})
    return {"saved": True, "filename": filename, "mtime": target.stat().st_mtime, "bytes": target.stat().st_size}


@router.post("/jobs/{record_id}/render")
def render_pdf(record_id: str = Depends(_require_job_id)) -> dict:
    """Synchronously re-render resume.pdf via render_resume.py."""
    row = _get_row_or_404(record_id)
    pkg_path = row.get("package_path") or ""
    pkg_dir, attempted_glob = _resolve_or_glob_package_dir(row)
    if pkg_dir is None:
        raise HTTPException(status_code=404, detail={"error": "package_not_found"})
    if attempted_glob and not pkg_path:
        jobs_repo.set_package_path(record_id, str(pkg_dir))
    resume_md = pkg_dir / "resume.md"
    if not resume_md.is_file():
        raise HTTPException(status_code=404, detail={"error": "resume_md_missing", "detail": str(resume_md)})
    if not RENDER_SCRIPT.is_file():
        raise HTTPException(status_code=500, detail={"error": "render_script_missing", "detail": str(RENDER_SCRIPT)})

    t0 = time.time()
    try:
        result = subprocess.run(
            ["python", str(RENDER_SCRIPT), str(resume_md)],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return {"rendered": False, "error": "timeout", "duration_ms": 60_000}
    duration_ms = int((time.time() - t0) * 1000)
    if result.returncode != 0:
        return {
            "rendered": False,
            "error":  "render_failed",
            "rc":     result.returncode,
            "stderr": result.stderr[-2000:],
            "stdout": result.stdout[-500:],
            "duration_ms": duration_ms,
        }
    pdf_path = _resume_pdf_path(pkg_dir)
    return {
        "rendered": True,
        "pdf_path": str(pdf_path) if pdf_path else None,
        "duration_ms": duration_ms,
    }


@router.get("/jobs/{record_id}/pdf")
def get_pdf(record_id: str = Depends(_require_job_id)):
    """Stream resume.pdf as application/pdf for the embedded preview."""
    row = _get_row_or_404(record_id)
    pkg_path = row.get("package_path") or ""
    pkg_dir, attempted_glob = _resolve_or_glob_package_dir(row)
    if pkg_dir is None:
        raise HTTPException(status_code=404, detail={"error": "package_not_found"})
    if attempted_glob and not pkg_path:
        jobs_repo.set_package_path(record_id, str(pkg_dir))
    pdf_path = _resume_pdf_path(pkg_dir)
    if pdf_path is None:
        raise HTTPException(status_code=404, detail={"error": "pdf_missing", "detail": "Run /render first"})
    return FileResponse(
        path=str(pdf_path),
        media_type="application/pdf",
        filename=pdf_path.name,
        headers={"Cache-Control": "no-store"},
    )


@router.patch("/jobs/{record_id}")
def patch_job(body: JobPatch, record_id: str = Depends(_require_job_id)) -> dict:
    """Partial-update job fields. Whitelist enforced in jobs_repo."""
    data = body.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(status_code=400, detail={"error": "empty_patch", "detail": "Provide at least one field"})
    try:
        updated = jobs_repo.patch_job(record_id, data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail={"error": "bad_patch", "detail": str(e)})
    if updated is None:
        raise HTTPException(status_code=404, detail={"error": "not_found", "detail": f"job {record_id} not found"})
    return updated


@router.delete("/jobs/{record_id}")
def delete_job(record_id: str = Depends(_require_job_id)) -> dict:
    """Hard-delete a single job row. Irreversible.

    Does not touch the Postgres seen_jobs dedup ledger; the scraper will continue
    to skip this source id on future runs, which is the desired behavior for a
    deliberate delete."""
    deleted = jobs_repo.delete_job(record_id)
    if not deleted:
        raise HTTPException(status_code=404, detail={"error": "not_found", "record_id": record_id})
    log.info("deleted job job_id=%s", record_id)
    return {"deleted": True, "record_id": record_id}


@router.post("/jobs/purge-rejects")
def purge_rejects(dry_run: bool = Query(False)) -> dict:
    """Hard-delete every row where Tier = 'Reject'. Irreversible."""
    ids = jobs_repo.list_reject_ids()
    if dry_run:
        return {"would_delete": len(ids), "ids": ids, "dry_run": True}
    deleted = jobs_repo.delete_rejects()
    log.info("purge_rejects: deleted=%d", deleted)
    return {"deleted": deleted, "ids": ids}


@router.post("/jobs/{record_id}/build")
def build_one(record_id: str = Depends(_require_job_id),
              no_pdf: bool = Query(False)) -> dict:
    """Trigger a one-shot build for a single job. Respects the shared _BUILD_LOCK
    so cron + manual triggers can't double-bill the Anthropic API."""
    acquired = _BUILD_LOCK.acquire(blocking=False)
    if not acquired:
        raise HTTPException(
            status_code=409,
            detail={"error": "build_in_progress",
                    "detail": "A build is already running; try again in a few minutes."},
        )
    try:
        import build_packages as bp  # noqa: WPS433, lazy so module load doesn't pin
        try:
            return bp.build_one_job(record_id, no_pdf=no_pdf)
        except bp.BuildGateError as e:
            raise HTTPException(status_code=409, detail={"error": e.code, "detail": str(e)})
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("build_one_job raised")
            raise HTTPException(status_code=500, detail={"error": "build_exception", "detail": str(e)[:400]})
    finally:
        _BUILD_LOCK.release()
