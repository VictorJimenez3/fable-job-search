"""Shared, bounded HTTP client with SSRF-safe redirects and polite retries."""

from __future__ import annotations

import ipaddress
import random
import socket
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import requests

UA = "JobRadar/2.0 (personal new-grad job monitor; github.com/VictorJimenez3/fable-job-search)"
TIMEOUT = 20
MAX_ATTEMPTS = 3
MAX_REDIRECTS = 5
RETRY_STATUSES = {429, 500, 502, 503, 504}

_session = requests.Session()
_session.headers.update({"User-Agent": UA, "Accept": "application/json,text/html,text/plain,*/*"})


class UnsafeURL(ValueError):
    """Raised before requesting a URL that can reach a non-public network."""


def _public_url(url: str) -> str:
    parsed = urlsplit(str(url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UnsafeURL("only absolute http(s) URLs are allowed")
    if parsed.username or parsed.password:
        raise UnsafeURL("URL credentials are not allowed")
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM
            )
        }
    except socket.gaierror as exc:
        raise UnsafeURL("hostname could not be resolved") from exc
    if not addresses:
        raise UnsafeURL("hostname did not resolve")
    for address in addresses:
        try:
            public = ipaddress.ip_address(address).is_global
        except ValueError as exc:
            raise UnsafeURL("hostname resolved to an invalid address") from exc
        if not public:
            raise UnsafeURL("private, loopback, link-local, and reserved networks are blocked")
    return parsed.geturl()


def _retry_delay(response: requests.Response | None, attempt: int) -> float:
    value = response.headers.get("Retry-After", "") if response is not None else ""
    if value:
        try:
            return min(30.0, max(0.0, float(value)))
        except ValueError:
            try:
                target = parsedate_to_datetime(value).timestamp()
                return min(30.0, max(0.0, target - time.time()))
            except (TypeError, ValueError, OverflowError):
                pass
    return min(8.0, (0.5 * (2 ** (attempt - 1))) + random.uniform(0.0, 0.25))


def _request_once(method: str, url: str, **kwargs) -> requests.Response:
    current = _public_url(url)
    follow = bool(kwargs.pop("allow_redirects", True))
    for redirect in range(MAX_REDIRECTS + 1):
        response = _session.request(method, current, allow_redirects=False, **kwargs)
        if not follow or response.status_code not in {301, 302, 303, 307, 308}:
            return response
        location = response.headers.get("Location", "")
        if not location:
            return response
        if redirect >= MAX_REDIRECTS:
            raise requests.TooManyRedirects("too many redirects")
        current = _public_url(urljoin(current, location))
        if response.status_code == 303 and method.upper() != "HEAD":
            method = "GET"
            kwargs.pop("data", None)
            kwargs.pop("json", None)
    raise requests.TooManyRedirects("too many redirects")


def _do(method: str, url: str, **kwargs):
    kwargs.setdefault("timeout", TIMEOUT)
    last_error: requests.RequestException | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        response = None
        try:
            response = _request_once(method, url, **kwargs)
            if response.status_code not in RETRY_STATUSES or attempt == MAX_ATTEMPTS:
                return response
        except UnsafeURL:
            raise
        except requests.RequestException as exc:
            last_error = exc
            if attempt == MAX_ATTEMPTS:
                raise
        time.sleep(_retry_delay(response, attempt))
    if last_error:
        raise last_error
    raise requests.RequestException("request failed without a response")


def get(url: str, **kwargs) -> requests.Response:
    return _do("GET", url, **kwargs)


def get_json(url: str, **kwargs):
    response = _do("GET", url, **kwargs)
    response.raise_for_status()
    return response.json()


def get_text(url: str, **kwargs) -> str:
    response = _do("GET", url, **kwargs)
    response.raise_for_status()
    return response.text


def post_json(url: str, payload: dict, **kwargs):
    kwargs.setdefault("headers", {})
    kwargs["headers"].setdefault("Content-Type", "application/json")
    response = _do("POST", url, json=payload, **kwargs)
    response.raise_for_status()
    return response.json()
