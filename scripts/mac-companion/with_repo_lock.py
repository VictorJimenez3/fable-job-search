#!/usr/bin/env python3
"""Run one companion command while holding the checkout-scoped lock.

The enrichment and social launch agents share one clone. The lock lives next
to that clone (rather than inside it), so it never appears as a dirty git
path, and is derived from the resolved repository path so separate clones do
not block one another. Contention is a normal launchd overlap: skip with
success. Lock setup failures are operational errors and fail closed.
"""
from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from pathlib import Path


def _usage() -> int:
    print("usage: with_repo_lock.py --repo PATH -- COMMAND [ARGS...]", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        separator = args.index("--")
    except ValueError:
        return _usage()
    option_args, command = args[:separator], args[separator + 1:]
    if (len(option_args) != 2 or option_args[0] != "--repo"
            or not option_args[1] or not command):
        return _usage()
    repo = Path(option_args[1]).expanduser().resolve()
    lock_path = repo.parent / f"{repo.name}-repo.lock"
    try:
        handle = lock_path.open("a+", encoding="utf-8")
    except OSError as exc:
        print(f"repo-lock: cannot open {lock_path}: {exc}", file=sys.stderr)
        return 2
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"repo-lock: busy; skipping {repo}")
            return 0
        except OSError as exc:
            print(f"repo-lock: cannot lock {lock_path}: {exc}", file=sys.stderr)
            return 2
        child_env = os.environ.copy()
        child_env["JOBRADAR_REPO_LOCK_HELD"] = "1"
        try:
            completed = subprocess.run(command, cwd=repo, env=child_env, check=False)
        except OSError as exc:
            print(f"repo-lock: cannot execute child: {exc}", file=sys.stderr)
            return 2
        return int(completed.returncode)
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
