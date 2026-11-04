#!/usr/bin/env python3
"""Run one bounded Instagram collection and publish only social state.

The ordinary enrichment companion owns the job/culture snapshots.  This
wrapper stages only ``state/social.json`` so a social run cannot overwrite a
crawl, internship evidence, or LLM cache.  A failed push leaves the local
social commit intact for the next hourly attempt.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _repo_dir() -> Path:
    return Path(os.environ.get("RADAR_DIR", Path(__file__).resolve().parents[2])).resolve()


def _run(repo: Path, *command: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 - command is a fixed git/python argv
        command, cwd=repo, capture_output=True, text=True,
        timeout=timeout, check=False,
    )


def _log(message: str) -> None:
    print(f"social-companion: {message}", flush=True)


def _unrelated_changes(repo: Path) -> list[str] | None:
    status = _run(repo, "git", "status", "--porcelain", timeout=15)
    if status.returncode != 0:
        return None
    return [
        line for line in status.stdout.splitlines()
        if line and "state/social.json" not in line
    ]


def _sync_before_collect(repo: Path, branch: str) -> bool:
    """Fetch/rebase only a clean clone; never reset crawler or enrich work."""
    unrelated = _unrelated_changes(repo)
    if unrelated is None:
        _log("could not inspect git status; keeping social state local")
        return False
    if unrelated:
        _log("deferred collection because another companion has worktree changes")
        return False
    status = _run(repo, "git", "status", "--porcelain", timeout=15)
    if status.returncode != 0:
        _log("could not inspect git status; keeping social state local")
        return False
    if status.stdout.strip():
        # An uncommitted social snapshot belongs to this lane. Preserve it and
        # let the collector merge new posts atomically before publishing.
        return True
    fetched = _run(repo, "git", "fetch", "origin", branch, timeout=45)
    if fetched.returncode != 0:
        _log("could not fetch before collection; keeping local state")
        return True
    rebased = _run(repo, "git", "rebase", f"origin/{branch}", timeout=45)
    if rebased.returncode != 0:
        _run(repo, "git", "rebase", "--abort", timeout=15)
        _log("rebase before collection failed; keeping local social state")
    return True


def _unpushed_paths(repo: Path, branch: str) -> list[str] | None:
    diff = _run(repo, "git", "diff", "--name-only", f"origin/{branch}..HEAD", timeout=15)
    if diff.returncode != 0:
        return None
    return [line for line in diff.stdout.splitlines() if line]


def _publish(repo: Path, branch: str) -> None:
    status = _run(repo, "git", "status", "--porcelain", timeout=15)
    if status.returncode != 0:
        _log("could not inspect git status; leaving social state local")
        return
    unrelated = [
        line for line in status.stdout.splitlines()
        if line and "state/social.json" not in line
    ]
    if unrelated:
        _log("left social state local because the companion has unrelated worktree changes")
        return
    staged = _run(repo, "git", "add", "--", "state/social.json", timeout=15)
    if staged.returncode != 0:
        _log(f"could not stage social state: {staged.stderr.strip()[-300:]}")
        return
    diff = _run(repo, "git", "diff", "--cached", "--quiet", timeout=15)
    if diff.returncode == 0:
        _log("no new social state changes")
    elif diff.returncode == 1:
        commit = _run(
            repo,
            "git", "-c", "user.name=job-radar-social",
            "-c", "user.email=radar-social@users.noreply.github.com",
            "commit", "-m", "radar: hourly Instagram social state",
            timeout=30,
        )
        if commit.returncode != 0:
            _log(f"could not commit social state: {commit.stderr.strip()[-300:]}")
            return
    else:
        _log("could not inspect staged social state; leaving it local")
        return
    for attempt in range(3):
        paths = _unpushed_paths(repo, branch)
        if paths is None:
            _log("could not inspect unpushed paths; leaving social state local")
            return
        if paths and any(path != "state/social.json" for path in paths):
            _log("left social state local because an unpushed non-social commit exists")
            return
        pushed = _run(repo, "git", "push", "origin", branch, timeout=45)
        if pushed.returncode == 0:
            _log("published state/social.json")
            return
        fetched = _run(repo, "git", "fetch", "origin", branch, timeout=45)
        if fetched.returncode != 0:
            break
        rebased = _run(repo, "git", "rebase", f"origin/{branch}", timeout=45)
        if rebased.returncode != 0:
            _run(repo, "git", "rebase", "--abort", timeout=15)
            break
        _log(f"push retry {attempt + 1}")
    _log("push deferred; local social commit is retained for the next cycle")


def _run_locked(repo: Path) -> int:
    sys.path.insert(0, str(repo))
    from radar.social import collect_social_leads

    branch_result = _run(repo, "git", "rev-parse", "--abbrev-ref", "HEAD", timeout=15)
    branch = os.environ.get("RADAR_BRANCH", "").strip() or branch_result.stdout.strip()
    if not branch or branch_result.returncode != 0:
        _log("could not determine branch; state remains local")
        return 0
    if not _sync_before_collect(repo, branch):
        return 0
    result = collect_social_leads()
    _log(json.dumps(result.get("stats", {}), sort_keys=True))
    _publish(repo, branch)
    return 0


def main() -> int:
    repo = _repo_dir()
    # The enrichment companion and this hourly lane share one checkout.  Use
    # the same non-blocking repository lock as run.sh before any fetch/rebase;
    # the environment marker prevents recursion when the helper starts us.
    if os.environ.get("JOBRADAR_REPO_LOCK_HELD") != "1":
        lock_script = repo / "scripts" / "mac-companion" / "with_repo_lock.py"
        if not lock_script.is_file():
            _log("repo lock helper is missing; refusing to collect")
            return 2
        completed = subprocess.run(  # noqa: S603 - fixed local lock wrapper and script path
            [sys.executable, str(lock_script), "--repo", str(repo), "--",
             sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]],
            cwd=repo, check=False,
        )
        return int(completed.returncode)
    return _run_locked(repo)


if __name__ == "__main__":
    raise SystemExit(main())
