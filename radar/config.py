"""Load profile.yaml + environment. Every module gets config through here."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(os.environ.get("RADAR_ROOT", Path(__file__).resolve().parent.parent))
STATE_DIR = Path(os.environ.get("RADAR_STATE_DIR", ROOT / "state"))
DOCS_DIR = Path(os.environ.get("RADAR_DOCS_DIR", ROOT / "docs"))
DATA_DIR = ROOT / "data"

_profile_cache: dict | None = None


def profile() -> dict:
    global _profile_cache
    if _profile_cache is None:
        with open(ROOT / "profile.yaml") as f:
            _profile_cache = yaml.safe_load(f)
    return _profile_cache


def seeds() -> list[dict]:
    with open(DATA_DIR / "companies_seed.yaml") as f:
        return yaml.safe_load(f)["companies"]


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def github_repo() -> str:
    return env("GITHUB_REPOSITORY", "VictorJimenez3/fable-job-search")


def github_owner() -> str:
    return github_repo().split("/")[0]
