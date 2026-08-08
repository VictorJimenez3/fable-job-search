"""Load profile.yaml + environment. Every module gets config through here."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(os.environ.get("RADAR_ROOT", Path(__file__).resolve().parent.parent))
STATE_DIR = Path(os.environ.get("RADAR_STATE_DIR", ROOT / "state"))
DOCS_DIR = Path(os.environ.get("RADAR_DOCS_DIR", ROOT / "docs"))
DATA_DIR = ROOT / "data"

_profile_cache: dict[str, dict] = {}


def profile_id() -> str:
    """Return the normalized board lane for this process.

    The historical deployment used ``default`` (and the ChemE branch uses
    ``cheme``). Keep both spellings working while giving the main platform
    stable lane identifiers for state, workflows, and API payloads.
    """
    value = env("RADAR_PROFILE", "new_grad").strip().lower()
    return {"": "new_grad", "default": "new_grad", "intern": "internship"}.get(value, value)


def profile() -> dict:
    mode = profile_id()
    if mode not in _profile_cache:
        path = ROOT / "profiles" / f"{mode}.yaml"
        if not path.exists():
            path = ROOT / "profile.yaml"
        with open(path) as f:
            _profile_cache[mode] = yaml.safe_load(f) or {}
    return _profile_cache[mode]


def seeds() -> list[dict]:
    with open(DATA_DIR / "companies_seed.yaml") as f:
        return yaml.safe_load(f)["companies"]


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def github_repo() -> str:
    return env("GITHUB_REPOSITORY", "VictorJimenez3/fable-job-search")


def github_owner() -> str:
    return github_repo().split("/")[0]
