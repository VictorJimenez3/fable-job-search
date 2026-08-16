"""Bounded Postgres work-queue runner for long or retryable operations."""

from __future__ import annotations

import os
import socket
import uuid
from collections.abc import Callable

from . import main
from .db.repository import RadarRepository, repository


def handlers() -> dict[str, Callable[[dict], object]]:
    return {
        "crawl.run": lambda _payload: main.crawl(),
        "score.recompute": lambda _payload: main.rescore_cmd(),
        "lifecycle.reconcile": lambda _payload: main.lifecycle_cmd(),
        "enrich.jobs": lambda _payload: main.enrich(),
        "tracker.sync": lambda _payload: main.tracker_sync(),
        "notify.dispatch": lambda _payload: main.deliver_alerts(),
    }


def worker_name() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"[:120]


def run_one(
    repo: RadarRepository | None = None,
    *,
    owner: str = "",
    available_handlers: dict[str, Callable[[dict], object]] | None = None,
) -> bool:
    repo = repo or repository()
    owner = owner or worker_name()
    item = repo.lease_work(owner)
    if not item:
        return False
    error = ""
    try:
        operation = (available_handlers or handlers()).get(str(item["kind"]))
        if not operation:
            raise ValueError(f"unsupported work kind: {item['kind']}")
        result = operation(dict(item.get("payload") or {}))
        if result not in (None, 0, True):
            raise RuntimeError(f"operation returned {result!r}")
    except Exception as exc:  # queue boundary owns retry classification
        error = f"{type(exc).__name__}: {exc}"
    repo.finish_work(item["id"], owner, error=error)
    return True
