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


def test_both_local_and_api_lanes_are_probed_and_local_is_selected(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("LLM_MODEL", "qwen3:30b")
    monkeypatch.setenv("NVIDIA_GLM_52_API_KEY", "glm-secret")
    monkeypatch.setenv("NVIDIA_GLM_52_MODEL", "z-ai/glm-5.2")
    monkeypatch.setenv("RADAR_AI_PROVIDER_ATTEMPTS", "1")
    seen = []

    def post(url, **kwargs):
        seen.append(url)
        if url.endswith("/api/chat"):
            return Response()
        return _Resp(200)

    monkeypatch.setattr(llm.requests, "post", post)
    assert llm.complete("use both", task="quality") == "useful answer"
    deadline = __import__("time").time() + 1
    while len(seen) < 2 and __import__("time").time() < deadline:
        __import__("time").sleep(0.01)
    report = llm.usage_report()
    assert set(seen) == {"http://localhost:11434/api/chat",
                         "https://integrate.api.nvidia.com/v1/chat/completions"}
    assert {e["lane"] for e in report["events"] if e["status"] == "ok"} == {"local", "api"}
    assert sum(1 for e in report["events"] if e.get("selected")) == 1


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


def test_global_request_pacer_smooths_provider_races(monkeypatch):
    monkeypatch.setenv("RADAR_AI_MAX_REQUESTS", "10")
    monkeypatch.setenv("RADAR_AI_REQUESTS_PER_MINUTE", "30")
    llm.reset_runtime()
    ticks = iter([10.0, 10.0])
    waits = []
    monkeypatch.setattr(llm.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(llm.time, "sleep", waits.append)
    llm._reserve_request()
    llm._reserve_request()
    assert waits == [2.0]


def _named_env(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.setenv("NVIDIA_API_BASE_URL", "https://integrate.api.nvidia.com/v1")
    monkeypatch.setenv("NVIDIA_GLM_52_API_KEY", "glm-secret")
    monkeypatch.setenv("NVIDIA_GLM_52_MODEL", "z-ai/glm-5.2")
    monkeypatch.setenv("NVIDIA_DEEPSEEK_V4_PRO_API_KEY", "deep-secret")
    monkeypatch.setenv("NVIDIA_DEEPSEEK_V4_PRO_MODEL", "deepseek-ai/deepseek-v4-pro")


def test_named_router_falls_back_after_unavailable_model(monkeypatch):
    _named_env(monkeypatch)
    seen = []

    def post(url, **kwargs):
        model = kwargs["json"]["model"]
        seen.append(model)
        return _Resp(404 if model.startswith("z-ai/") else 200)

    monkeypatch.setattr(llm.requests, "post", post)
    assert llm.complete("grade", task="quality") == "graded"
    assert set(seen) == {"z-ai/glm-5.2", "deepseek-ai/deepseek-v4-pro"}
    report = llm.usage_report()
    assert report["requests"] == 2 and report["models"]["nvidia:deepseek"]["ok"] == 1


def test_budget_is_hard_and_telemetry_never_contains_keys(monkeypatch):
    _named_env(monkeypatch)
    monkeypatch.setenv("RADAR_AI_MAX_CALLS", "1")
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: _Resp(200))
    assert llm.complete("first", task="quality") == "graded"
    assert llm.complete("second", task="quality") is None
    report = llm.usage_report()
    assert report["logical_calls"] == 1
    assert report["events"][-1]["status"] == "budget_skipped"
    serialized = str(report)
    assert "glm-secret" not in serialized and "deep-secret" not in serialized


def test_schema_failure_falls_through_within_one_logical_call(monkeypatch):
    _named_env(monkeypatch)
    responses = iter([
        _Resp(200),
        _Resp(200),
    ])
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: next(responses))
    seen = {"n": 0}

    def valid(text):
        seen["n"] += 1
        return seen["n"] == 2

    assert llm.complete("json", task="quality", validator=valid) == "graded"
    assert llm.usage_report()["logical_calls"] == 1


def test_all_configured_api_providers_are_started_concurrently(monkeypatch):
    _named_env(monkeypatch)
    monkeypatch.setenv("NVIDIA_NEMOTRON_3_ULTRA_550B_A55B_API_KEY", "nemotron-secret")
    monkeypatch.setenv("NVIDIA_NEMOTRON_3_ULTRA_550B_A55B_MODEL", "nvidia/nemotron")
    monkeypatch.setenv("NVIDIA_KIMI_K2_6_API_KEY", "kimi-secret")
    monkeypatch.setenv("NVIDIA_KIMI_K2_6_MODEL", "moonshotai/kimi")
    started = []
    release = __import__("threading").Event()

    def post(url, **kwargs):
        started.append(kwargs["json"]["model"])
        release.wait(0.05)
        return _Resp(200)

    monkeypatch.setattr(llm.requests, "post", post)
    assert llm.complete("race", task="quality") == "graded"
    assert set(started) == {"z-ai/glm-5.2", "deepseek-ai/deepseek-v4-pro",
                            "nvidia/nemotron", "moonshotai/kimi"}
