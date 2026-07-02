"""Runtime config for the Job Pipeline service."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load .env if present (useful for local dev; in compose, env_file already set vars)
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

VERSION = "1.0.0"

# The pipeline package ships inside this repo (copied to /app/pipeline in the
# image). PIPELINE_PATH env var overrides for unusual layouts.
REPO_ROOT     = Path(__file__).resolve().parents[1]
PIPELINE_PATH = Path(os.environ.get("PIPELINE_PATH", str(REPO_ROOT / "pipeline")))

# Where built application packages are written. The compose file bind-mounts
# ./data/applications here so packages are visible on the host.
APPLICATIONS_DIR = Path(os.environ.get("APPLICATIONS_DIR", "/data/applications"))

# Your profile (search config, digest, templates). Mounted read-only in compose.
PROFILE_DIR = Path(os.environ.get("PROFILE_DIR", str(REPO_ROOT / "profile")))

# Make the pipeline package importable (jobs_repo, seen_jobs_db, build_packages)
# so the service can talk to Postgres directly.
if str(PIPELINE_PATH) not in sys.path:
    sys.path.insert(0, str(PIPELINE_PATH))

API_BEARER_TOKEN  = os.environ.get("API_BEARER_TOKEN", "").strip()
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
TZ                = os.environ.get("TZ", "America/New_York")
