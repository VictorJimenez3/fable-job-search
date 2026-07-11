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
