"""Shared HTTP helpers: one session, sane timeouts, single retry, never raises past the caller's except."""
from __future__ import annotations

import time

import requests

UA = "JobRadar/1.0 (personal chemical-engineering internship monitor; github.com/VictorJimenez3/fable-job-search)"
TIMEOUT = 20

_session = requests.Session()
_session.headers.update({"User-Agent": UA, "Accept": "application/json,text/html,text/plain,*/*"})


def _do(method: str, url: str, **kw):
    kw.setdefault("timeout", TIMEOUT)
    last = None
    for attempt in (1, 2):
        try:
            r = _session.request(method, url, **kw)
            if r.status_code >= 500 and attempt == 1:
                time.sleep(1.5)
                continue
            return r
        except requests.RequestException as e:
            last = e
            if attempt == 1:
                time.sleep(1.5)
    raise last


def get(url: str, **kw) -> requests.Response:
    return _do("GET", url, **kw)


def get_json(url: str, **kw):
    r = _do("GET", url, **kw)
    r.raise_for_status()
    return r.json()


def get_text(url: str, **kw) -> str:
    r = _do("GET", url, **kw)
    r.raise_for_status()
    return r.text


def post_json(url: str, payload: dict, **kw):
    kw.setdefault("headers", {})
    kw["headers"].setdefault("Content-Type", "application/json")
    r = _do("POST", url, json=payload, **kw)
    r.raise_for_status()
    return r.json()
