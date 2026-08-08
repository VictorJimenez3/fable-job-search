#!/usr/bin/env python3
"""Owner-controlled lock for the local CV reference artifacts.

The macOS ``uchg`` flag and read-only permissions stop normal applications and
agents from replacing the protected files.  Deliberate edits require running
``unlock`` interactively and entering the owner PIN; ``lock`` restores the
protection afterward.

This is an operational guard, not a security boundary against a privileged
local process.  An agent with unrestricted terminal access can intentionally
clear filesystem flags, so the PIN is a human confirmation step rather than a
cryptographic guarantee against the machine owner.
"""

from __future__ import annotations

import getpass
import hashlib
import hmac
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable, Optional


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CV_ROOT = REPO_ROOT / "CV"
PROTECTED_RELATIVE_PATHS = (
    Path("immutable/VictorJimenezResume.tex"),
    Path("immutable/VictorJimenezResume.pdf"),
    Path("immutable/og_resume.tex"),
    Path("immutable/og_resume.pdf"),
    Path("immutable/tldp_resume.tex"),
    Path("immutable/tldp_resume.pdf"),
)
PIN_SALT = bytes.fromhex("c2e0c9d4f71706f4eddba2a7e8a2ae56")
PIN_DIGEST = bytes.fromhex("39416156402b913abc71c995623bec926bd4f4c3d89ab0e0f37f6bbc0de2d074")
PIN_ITERATIONS = 600_000


def protected_paths(cv_root: Optional[Path] = None) -> Iterable[Path]:
    base = Path(cv_root or DEFAULT_CV_ROOT).resolve()
    return tuple(base / relative for relative in PROTECTED_RELATIVE_PATHS)


def _verify_pin(pin: str) -> bool:
    candidate = hashlib.pbkdf2_hmac(
        "sha256", pin.encode("utf-8"), PIN_SALT, PIN_ITERATIONS
    )
    return hmac.compare_digest(candidate, PIN_DIGEST)


def _chflags_available() -> bool:
    return sys.platform == "darwin" and shutil.which("chflags") is not None


def _set_flags(flag: str, paths: Iterable[Path]) -> None:
    if not _chflags_available():
        return
    subprocess.run(
        ["chflags", flag, *(str(path) for path in paths)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


def _immutable_directory(cv_root: Optional[Path] = None) -> Path:
    return Path(cv_root or DEFAULT_CV_ROOT).resolve() / "immutable"


def lock_files(cv_root: Optional[Path] = None) -> Dict[str, object]:
    """Apply read-only permissions and macOS user-immutable flags."""
    paths = tuple(protected_paths(cv_root))
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise FileNotFoundError("protected CV files missing: " + ", ".join(missing))
    directory = _immutable_directory(cv_root)
    _set_flags("nouchg", (*paths, directory))
    for path in paths:
        os.chmod(path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    os.chmod(directory, stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    _set_flags("uchg", (*paths, directory))
    return lock_status(cv_root)


def unlock_files(pin: str, cv_root: Optional[Path] = None) -> Dict[str, object]:
    """Unlock after verifying the owner PIN; never accepts a PIN in argv."""
    if not _verify_pin(pin):
        raise PermissionError("incorrect owner PIN; protected CV files remain locked")
    paths = tuple(protected_paths(cv_root))
    directory = _immutable_directory(cv_root)
    _set_flags("nouchg", (directory, *paths))
    os.chmod(directory, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    for path in paths:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
    return lock_status(cv_root)


def lock_status(cv_root: Optional[Path] = None) -> Dict[str, object]:
    paths = tuple(protected_paths(cv_root))
    files = []
    for path in paths:
        exists = path.is_file()
        mode_locked = exists and not bool(path.stat().st_mode & stat.S_IWUSR)
        flag_locked = _user_immutable(path) if exists else False
        files.append({"path": str(path), "exists": exists, "mode_locked": mode_locked, "flag_locked": flag_locked})
    directory = _immutable_directory(cv_root)
    mode_locked = directory.is_dir() and not bool(directory.stat().st_mode & stat.S_IWUSR)
    flag_locked = _user_immutable(directory) if directory.is_dir() else False
    return {
        "locked": bool(files) and all(item["exists"] and item["mode_locked"] and item["flag_locked"] for item in files) and mode_locked and flag_locked,
        "filesystem_flags": _chflags_available(),
        "directory": str(directory),
        "files": files,
    }


def _user_immutable(path: Path) -> bool:
    """Return the macOS user-immutable flag when the platform exposes it."""
    if not _chflags_available():
        return True
    flag = getattr(stat, "UF_IMMUTABLE", 0)
    return bool(flag and getattr(path.stat(), "st_flags", 0) & flag)


def main(argv: Optional[list[str]] = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    command = args[0] if args else "status"
    if command == "status":
        print(lock_status())
        return 0
    if command == "lock":
        print(lock_files())
        return 0
    if command == "unlock":
        if len(args) != 1:
            print("PIN is entered interactively; do not pass it as an argument.", file=sys.stderr)
            return 2
        try:
            pin = getpass.getpass("Owner PIN: ")
            print(unlock_files(pin))
        except (PermissionError, FileNotFoundError, OSError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0
    print("usage: resume_lock.py [status|lock|unlock]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
