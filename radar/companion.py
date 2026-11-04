"""Portable companion operator for Dot and the Mac fallback.

This module is a small control surface. Existing companion scripts still own
mutations; this module checks whether a dedicated checkout is safe to use and,
after explicit opt-in, invokes one of those scripts.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows cannot execute POSIX companion
    fcntl = None  # type: ignore[assignment]

DEFAULT_BRANCH = "claude/newgrad-job-search-system-9gbj9k"
RUNTIMES = {"dot", "mac"}
OPERATIONS = {"social", "enrich"}


def repo_dir() -> Path:
    configured = os.environ.get("RADAR_DIR", "").strip()
    return Path(configured).expanduser().resolve() if configured else Path(__file__).resolve().parents[1]


def expected_branch() -> str:
    return os.environ.get("RADAR_BRANCH", "").strip() or DEFAULT_BRANCH


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        env = os.environ.copy()
        env["GIT_OPTIONAL_LOCKS"] = "0"
        return subprocess.run(  # noqa: S603, S607 - fixed git executable and bounded operator args
            ["git", "-C", str(repo), *args],  # noqa: S607
            env=env, capture_output=True,
            text=True, check=False, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(["git", *args], 127, "", str(exc))


def _git_value(repo: Path, *args: str) -> str | None:
    result = _git(repo, *args)
    if result.returncode:
        return None
    value = result.stdout.strip()
    return value or None


def _git_operation_in_progress(repo: Path) -> bool | None:
    paths: list[Path] = []
    for name in ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "sequencer"):
        value = _git_value(repo, "rev-parse", "--git-path", name)
        if value is None:
            return None
        path = Path(value)
        paths.append(path if path.is_absolute() else repo / path)
    return any(path.exists() for path in paths)


def _unpublished(repo: Path, branch: str) -> int | None:
    if _git_value(repo, "rev-parse", "--verify", f"origin/{branch}") is None:
        return None
    result = _git(repo, "rev-list", "--count", f"origin/{branch}..HEAD")
    try:
        return int(result.stdout.strip()) if result.returncode == 0 else None
    except ValueError:
        return None


def doctor(repo: Path | None = None) -> dict[str, Any]:
    """Return an allowlisted, read-only capability report."""
    checkout = (repo or repo_dir()).resolve()
    repo_root = _git_value(checkout, "rev-parse", "--show-toplevel") if shutil.which("git") else None
    git_ok = repo_root is not None
    branch = _git_value(checkout, "symbolic-ref", "--quiet", "--short", "HEAD") if git_ok else None
    expected = expected_branch()
    status = _git(checkout, "status", "--porcelain") if git_ok else None
    operation = _git_operation_in_progress(checkout) if git_ok else None
    unpublished = _unpublished(checkout, expected) if git_ok and branch else None
    binaries = {name: shutil.which(name) is not None for name in ("bash", "git", "opencli", "ollama")}
    python_ok = sys.version_info >= (3, 12)
    checks = {
        "python312": python_ok,
        "posix": os.name == "posix",
        "git": git_ok and binaries["git"],
        "clean_checkout": status is not None and status.returncode == 0 and not status.stdout.strip(),
        "expected_branch": bool(branch and branch == expected),
        "repository_root": bool(repo_root and Path(repo_root).resolve() == checkout),
        "no_git_operation": operation is False,
        "no_unpublished_commits": unpublished == 0,
    }
    return {
        "schema_version": 1,
        "host_os": platform.system().lower() or "unknown",
        "git": {
            "repository": git_ok,
            "branch_attached": bool(branch),
            "branch_matches_expected": bool(branch and branch == expected),
            "clean": checks["clean_checkout"],
            "operation_in_progress": operation,
            "unpublished_commits": unpublished,
            "remote_ref_freshness": "unverified",
        },
        "capabilities": {
            "python312": python_ok,
            "posix": os.name == "posix",
            "bash": binaries["bash"],
            "git": binaries["git"],
            "opencli_on_path": binaries["opencli"],
            "ollama_on_path": binaries["ollama"],
            "enrichment_backend": "ollama",
            "browser_auth": "unverified",
            "network": "unverified",
            "github_push": "unverified",
            "llm": "unverified",
            "schedule": "unverified",
        },
        "checks": checks,
        "ready_for_cutover": False,
        "notes": [
            "Dot browser authentication and scheduling are not verified by this command.",
            "Mac must be disabled before Dot becomes the sole mutating companion.",
        ],
    }


def _safe_to_execute(repo: Path, runtime: str) -> tuple[bool, str]:
    if runtime not in RUNTIMES:
        return False, "runtime must be dot or mac"
    if os.environ.get("JOBRADAR_COMPANION_PRIMARY", "").strip().lower() != runtime:
        return False, "JOBRADAR_COMPANION_PRIMARY must explicitly select this runtime"
    report = doctor(repo)
    checks = report["checks"]
    required = (
        "python312", "posix", "git", "clean_checkout", "expected_branch",
        "repository_root", "no_git_operation", "no_unpublished_commits",
    )
    if not all(checks.get(name) for name in required):
        return False, "dedicated checkout is not clean, current, or fully inspectable"
    return True, ""


def _command(operation: str, repo: Path) -> list[str]:
    scripts = repo / "scripts" / "mac-companion"
    return [sys.executable, str(scripts / "social.py")] if operation == "social" else ["bash", str(scripts / "run.sh")]


def _with_lock(repo: Path):
    if fcntl is None:
        raise OSError("POSIX file locking is unavailable")
    lock_path = repo.parent / f"{repo.name}-repo.lock"
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return None
        return handle
    except Exception:
        handle.close()
        raise


def run_operation(
    operation: str,
    *,
    runtime: str,
    repo: Path | None = None,
    execute: bool = False,
    json_output: bool = False,
) -> dict[str, Any]:
    """Plan or explicitly execute one existing companion lane."""
    checkout = (repo or repo_dir()).resolve()
    if operation not in OPERATIONS:
        return {"ok": False, "operation": operation, "error": "operation must be social or enrich"}
    if runtime not in RUNTIMES:
        return {"ok": False, "operation": operation, "runtime": runtime, "error": "runtime must be dot or mac"}
    if not execute:
        return {
            "ok": True,
            "operation": operation,
            "runtime": runtime,
            "executed": False,
            "mutations": ["state/social.json"] if operation == "social" else ["generated enrichment state"],
            "requires": [
                "explicit primary runtime", "clean dedicated checkout",
                "verified browser/LLM credentials at runtime",
            ],
        }
    if not os.environ.get("RADAR_DIR", "").strip():
        return {
            "ok": False, "operation": operation, "runtime": runtime,
            "executed": False, "error": "RADAR_DIR is required for execution",
        }
    try:
        lock = _with_lock(checkout)
    except OSError:
        return {
            "ok": False, "operation": operation, "runtime": runtime,
            "executed": False, "error": "companion lock is unavailable",
        }
    if lock is None:
        return {
            "ok": False, "operation": operation, "runtime": runtime,
            "executed": False, "status": "skipped_busy", "error": "companion checkout is busy",
        }
    try:
        safe, reason = _safe_to_execute(checkout, runtime)
        if not safe:
            return {"ok": False, "operation": operation, "runtime": runtime, "executed": False, "error": reason}
        child_env = os.environ.copy()
        child_env.pop("JOBRADAR_REPO_LOCK_HELD", None)
        child_env["RADAR_DIR"] = str(checkout)
        child_env["RADAR_BRANCH"] = expected_branch()
        child_env["JOBRADAR_REPO_LOCK_HELD"] = "1"
        try:
            completed = subprocess.run(  # noqa: S603 - command is a fixed local companion script
                _command(operation, checkout), cwd=checkout, env=child_env,
                stdout=sys.stderr if json_output else None, text=True, check=False,
            )
        except OSError:
            return {
                "ok": False, "operation": operation, "runtime": runtime,
                "executed": True, "error": "companion command could not start",
            }
        return {
            "ok": completed.returncode == 0,
            "operation": operation,
            "runtime": runtime,
            "executed": True,
            "exit_code": completed.returncode,
            "command_completed": completed.returncode == 0,
            "outcome": "unverified",
        }
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m radar.companion")
    sub = parser.add_subparsers(dest="command", required=True)
    doc = sub.add_parser("doctor", help="read-only runtime capability report")
    doc.add_argument("--json", action="store_true", help="emit JSON")
    run = sub.add_parser("run", help="plan or explicitly run one companion lane")
    run.add_argument("operation", choices=sorted(OPERATIONS))
    run.add_argument("--runtime", choices=sorted(RUNTIMES), default="dot")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--execute", action="store_true")
    run.add_argument("--json", action="store_true", help="emit JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "doctor":
        value = doctor()
    elif args.execute and args.dry_run:
        value = {"ok": False, "error": "choose either --dry-run or --execute"}
    else:
        value = run_operation(args.operation, runtime=args.runtime, execute=args.execute, json_output=args.json)
    compact = getattr(args, "json", False)
    print(json.dumps(value, sort_keys=compact, indent=None if compact else 2))
    if args.command == "doctor":
        # Inspection completed; individual checks and unverified capabilities
        # are represented in JSON. This is never a readiness assertion.
        return 0
    if "exit_code" in value:
        code = int(value["exit_code"])
        return code if code >= 0 else 128 - code
    return 0 if value.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
