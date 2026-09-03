#!/usr/bin/env python3
"""Restricted Chrome native-messaging bridge for the Job Radar extension.

The host deliberately exposes only the two operations the extension needs:
trigger Simplify's documented keyboard shortcut and keep a plugged-in Mac
awake while automation is enabled. It never accepts shell commands, paths,
cookies, DOM data, or credentials from Chrome.
"""
from __future__ import annotations

import json
import os
import platform
import select
import struct
import subprocess
import sys
import threading
import time
import uuid
from urllib.parse import urlparse

ALLOWED_ACTIONS = {"health", "trigger_simplify", "power_hold", "power_release"}
power_process: subprocess.Popen[str] | None = None
write_lock = threading.Lock()


def write_message(value: dict) -> None:
    data = json.dumps(value, separators=(",", ":")).encode("utf-8")
    with write_lock:
        sys.stdout.buffer.write(struct.pack("<I", len(data)) + data)
        sys.stdout.buffer.flush()


def valid_web_url(value: object) -> bool:
    try:
        parsed = urlparse(str(value or ""))
        return parsed.scheme in {"http", "https"} and not parsed.username and not parsed.password and bool(parsed.netloc)
    except ValueError:
        return False


def trigger_simplify(message: dict) -> dict:
    current = str(message.get("tab_url") or "")
    expected = str(message.get("expected_url") or "")
    if not valid_web_url(current) or not valid_web_url(expected):
        raise ValueError("Simplify can only be triggered on a verified employer web page")
    if platform.system() != "Darwin":
        raise RuntimeError("The native Simplify trigger is supported on macOS only")
    # The extension already verified the exact tab identity. Accessibility is
    # used only to focus Chrome and send Simplify's own Alt+Shift+F command.
    script = [
        'tell application "Google Chrome" to activate',
        'tell application "System Events" to keystroke "f" using {option down, shift down}',
    ]
    result = subprocess.run(["osascript", "-e", script[0], "-e", script[1]], capture_output=True, text=True, timeout=5)
    if result.returncode:
        raise RuntimeError("macOS Accessibility could not send Simplify's shortcut; grant permission once in System Settings")
    return {"trigger_id": f"simplify-{uuid.uuid4().hex}", "triggered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def power_hold() -> dict:
    global power_process
    if platform.system() != "Darwin":
        return {"held": False, "reason": "macOS only"}
    if power_process and power_process.poll() is None:
        return {"held": True}
    # -i prevents idle sleep; it does not prevent lid-close or shutdown.
    power_process = subprocess.Popen(["caffeinate", "-dimsu", "-w", str(os.getpid())], text=True)
    return {"held": True}


def power_release() -> dict:
    global power_process
    if power_process and power_process.poll() is None:
        power_process.terminate()
    power_process = None
    return {"held": False}


def handle(message: dict) -> dict:
    action = str(message.get("action") or "").strip()
    if action not in ALLOWED_ACTIONS:
        raise ValueError("native action is not allowed")
    if action == "health":
        return {"ready": True, "host": "job-radar", "platform": platform.system()}
    if action == "trigger_simplify":
        return trigger_simplify(message)
    if action == "power_hold":
        return power_hold()
    return power_release()


def main() -> None:
    while True:
        header = sys.stdin.buffer.read(4)
        if len(header) != 4:
            break
        length = struct.unpack("<I", header)[0]
        if length <= 0 or length > 1_000_000:
            break
        raw = sys.stdin.buffer.read(length)
        if len(raw) != length:
            break
        try:
            message = json.loads(raw.decode("utf-8"))
            result = handle(message if isinstance(message, dict) else {})
            write_message({"ok": True, "request_id": message.get("request_id"), **result})
        except Exception as exc:  # return a safe, user-visible error to Chrome
            write_message({"ok": False, "request_id": (message or {}).get("request_id"), "error": str(exc)[:300]})


if __name__ == "__main__":
    main()
