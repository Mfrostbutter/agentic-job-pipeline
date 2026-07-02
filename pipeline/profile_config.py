"""profile_config.py - loads the user's profile (YAML config + markdown digest).

The profile directory holds everything personal:

    profile.yaml   search config, contact info, style rules (copy from profile.example.yaml)
    profile.md     free-form background digest the AI grounds every claim in
                   (copy from profile.example.md)
    resume-template.md   optional skeleton the writer follows
    resume-example.md    optional gold-standard resume the writer imitates

Resolution order for the directory: $PROFILE_DIR, else <repo>/profile.
If profile.yaml / profile.md are missing, the .example files are used and a
loud warning is printed, so the pipeline works out of the box but nobody
accidentally ships applications for "Alex Example".
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

PROFILE_DIR = Path(os.environ.get("PROFILE_DIR",
                                  str(Path(__file__).resolve().parents[1] / "profile")))


def _pick(name: str) -> Path:
    """Prefer the user's file; fall back to the shipped example with a warning."""
    real = PROFILE_DIR / name
    if real.exists():
        return real
    stem, dot, ext = name.rpartition(".")
    example = PROFILE_DIR / f"{stem}.example.{ext}"
    if example.exists():
        print(f"[warn] {real} not found; using {example.name}. "
              f"Copy it to {name} and personalize it.", file=sys.stderr)
        return example
    return real  # missing; callers handle


def load_profile() -> dict:
    """Parse profile.yaml (or the example). Returns {} if neither exists."""
    path = _pick("profile.yaml")
    if not path.exists():
        print(f"[warn] no profile.yaml or profile.example.yaml in {PROFILE_DIR}",
              file=sys.stderr)
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_digest() -> str:
    """Read profile.md (or the example). Returns a placeholder if missing."""
    path = _pick("profile.md")
    if not path.exists():
        return "[No profile digest found. Create profile/profile.md.]"
    return path.read_text(encoding="utf-8", errors="replace")


def read_optional(name: str) -> str:
    """Read an optional profile file ('' if absent). Checks .example fallback."""
    path = _pick(name)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def candidate(profile: dict) -> dict:
    return profile.get("candidate") or {}


def search_cfg(profile: dict) -> dict:
    return profile.get("search") or {}


def style_cfg(profile: dict) -> dict:
    return profile.get("style") or {}


def contact_block(profile: dict) -> str:
    """Plain-text contact block for prompt injection."""
    c = candidate(profile)
    lines = [c.get("name", "The Candidate")]
    loc = c.get("location", "")
    if loc:
        lines.append(loc)
    bits = [b for b in (c.get("email", ""), c.get("phone", "")) if b]
    if bits:
        lines.append("  ".join(bits))
    for link in c.get("links") or []:
        lines.append(str(link))
    return "\n".join(lines)


def contact_line_md(profile: dict) -> str:
    """One-line markdown contact line for the resume header."""
    c = candidate(profile)
    parts = []
    if c.get("location"):
        parts.append(c["location"])
    if c.get("email"):
        parts.append(f"[{c['email']}](mailto:{c['email']})")
    if c.get("phone"):
        parts.append(str(c["phone"]))
    for link in c.get("links") or []:
        link = str(link)
        parts.append(f"[{link}](https://{link.removeprefix('https://').removeprefix('http://')})")
    return "  ·  ".join(parts)
