from email.message import Message

import pytest
import requests

from radar import http


def test_public_url_rejects_credentials_and_non_http_schemes(monkeypatch):
    monkeypatch.setattr(
        http.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (http.socket.AF_INET, http.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        ],
    )
    assert http._public_url("https://example.com/jobs") == "https://example.com/jobs"
    with pytest.raises(http.UnsafeURL):
        http._public_url("javascript:alert(1)")
    with pytest.raises(http.UnsafeURL):
        http._public_url("https://user:pass@example.com/jobs")


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.8", "169.254.169.254", "::1", "fc00::1"])
def test_public_url_rejects_non_global_dns_answers(monkeypatch, address):
    family = http.socket.AF_INET6 if ":" in address else http.socket.AF_INET
    monkeypatch.setattr(
        http.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (family, http.socket.SOCK_STREAM, 6, "", (address, 443)),
        ],
    )
    with pytest.raises(http.UnsafeURL):
        http._public_url("https://example.com/jobs")


def test_redirect_target_is_revalidated(monkeypatch):
    answers = {
        "public.example": "93.184.216.34",
        "internal.example": "127.0.0.1",
    }
    monkeypatch.setattr(
        http.socket,
        "getaddrinfo",
        lambda host, *args, **kwargs: [
            (http.socket.AF_INET, http.socket.SOCK_STREAM, 6, "", (answers[host], 443)),
        ],
    )
    response = requests.Response()
    response.status_code = 302
    response.headers = Message()
    response.headers["Location"] = "https://internal.example/metadata"
    monkeypatch.setattr(http._session, "request", lambda *args, **kwargs: response)
    with pytest.raises(http.UnsafeURL):
        http._request_once("GET", "https://public.example/jobs")
