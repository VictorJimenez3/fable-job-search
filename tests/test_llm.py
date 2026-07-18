from radar import llm


class Response:
    def raise_for_status(self):
        pass

    def json(self):
        return {"message": {"content": "<think>reasoning</think>useful answer"}}


def test_local_ollama_uses_native_api_and_unloads(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("LLM_MODEL", "qwen3:30b")
    seen = {}

    def post(url, **kwargs):
        seen["url"] = url
        seen["body"] = kwargs["json"]
        return Response()

    monkeypatch.setattr(llm.requests, "post", post)

    assert llm.complete("rank this", max_tokens=123) == "useful answer"
    assert seen["url"] == "http://localhost:11434/api/chat"
    assert seen["body"]["keep_alive"] == 0
    assert seen["body"]["think"] is False
    assert seen["body"]["options"]["num_predict"] == 123


def test_non_ollama_compat_url_remains_supported(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("LLM_BASE_URL", "https://example.test/v1")
    assert llm._ollama_api_url("https://example.test/v1") is None


class RateLimited:
    """429 with Retry-After for the first n calls, then a normal answer."""
    def __init__(self, fails):
        self.fails = fails
        self.calls = 0

    def post(self, url, **kwargs):
        self.calls += 1
        if self.calls <= self.fails:
            return _Resp(429, {"Retry-After": "7"})
        return _Resp(200)


class _Resp:
    def __init__(self, status, headers=None):
        self.status_code = status
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return {"choices": [{"message": {"content": "graded"}}]}


def test_free_tier_429_retried_with_retry_after(monkeypatch):
    # NVIDIA NIM / AI Studio free keys rate-limit per minute — a 429 must not
    # burn a verification attempt when waiting a few seconds fixes it
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("LLM_BASE_URL", "https://integrate.api.nvidia.com/v1")
    monkeypatch.setenv("LLM_MODEL", "meta/llama-3.3-70b-instruct")
    srv = RateLimited(fails=1)
    waits = []
    monkeypatch.setattr(llm.requests, "post", srv.post)
    monkeypatch.setattr(llm.time, "sleep", waits.append)
    assert llm.complete("grade this") == "graded"
    assert srv.calls == 2
    assert waits == [7]                     # Retry-After honored


def test_persistent_429_still_degrades_to_none(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("LLM_BASE_URL", "https://integrate.api.nvidia.com/v1")
    srv = RateLimited(fails=99)
    monkeypatch.setattr(llm.requests, "post", srv.post)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    assert llm.complete("grade this") is None
    assert srv.calls == 1 + llm._MAX_RETRIES  # bounded, no spin
