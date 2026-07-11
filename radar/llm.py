"""Provider-agnostic LLM access. One entrypoint: complete(prompt).

Provider resolution, in order:
  1. ANTHROPIC_API_KEY set        -> Anthropic Messages API (model: profile llm.model)
  2. LLM_BASE_URL set             -> any OpenAI-compatible /chat/completions endpoint
                                     (Ollama at http://localhost:11434/v1, Google AI
                                     Studio's OpenAI-compat endpoint with a free key,
                                     LM Studio, vLLM, ...). Optional LLM_API_KEY,
                                     optional LLM_MODEL (defaults to profile
                                     llm.local_model).
  3. neither                      -> None; callers degrade to heuristics.

This is what lets the same enrichment code run on GitHub Actions (with a paid
key), on the user's M1 Max via Ollama (free, automatic when the laptop is on),
or not at all — without any caller caring which.
"""
from __future__ import annotations

import requests

from .config import env, profile

ANTHROPIC_API = "https://api.anthropic.com/v1/messages"


def provider() -> str | None:
    if env("ANTHROPIC_API_KEY"):
        return "anthropic"
    if env("LLM_BASE_URL"):
        return "openai-compatible"
    return None


def available() -> bool:
    return provider() is not None


def _strip_thinking(text: str) -> str:
    """Remove reasoning wrappers emitted by some local models."""
    if "</think>" in text:
        text = text.split("</think>")[-1]
    return text.strip()


def _ollama_api_url(base: str) -> str | None:
    """Return Ollama's native chat endpoint for a local OpenAI-compatible URL.

    The native endpoint supports ``keep_alive: 0``. That matters on a laptop:
    the server may stay available, but the (large) model is released from
    unified memory immediately after each enrichment request.
    """
    url = base.rstrip("/")
    if url in {"http://localhost:11434/v1", "http://127.0.0.1:11434/v1"}:
        return url[:-3] + "/api/chat"
    return None


def complete(prompt: str, max_tokens: int = 2000, timeout: int = 180) -> str | None:
    """Run a single-turn completion. Returns text, or None on any failure —
    callers must always have a heuristic fallback."""
    p = provider()
    try:
        if p == "anthropic":
            r = requests.post(ANTHROPIC_API, timeout=timeout, json={
                "model": profile()["llm"]["model"],
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            }, headers={"x-api-key": env("ANTHROPIC_API_KEY"),
                        "anthropic-version": "2023-06-01"})
            r.raise_for_status()
            return "".join(b.get("text", "") for b in r.json().get("content", []))
        if p == "openai-compatible":
            base = env("LLM_BASE_URL").rstrip("/")
            model = env("LLM_MODEL") or profile()["llm"].get("local_model", "qwen3:14b")
            ollama_url = _ollama_api_url(base)
            if ollama_url:
                r = requests.post(ollama_url, timeout=timeout, json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "think": False,
                    "keep_alive": 0,
                    "options": {"num_predict": max_tokens},
                })
                r.raise_for_status()
                return _strip_thinking((r.json().get("message") or {}).get("content") or "") or None
            headers = {"Content-Type": "application/json"}
            if env("LLM_API_KEY"):
                headers["Authorization"] = f"Bearer {env('LLM_API_KEY')}"
            r = requests.post(f"{base}/chat/completions", timeout=timeout, json={
                "model": model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            }, headers=headers)
            r.raise_for_status()
            choices = r.json().get("choices", [])
            if not choices:
                return None
            text = (choices[0].get("message") or {}).get("content") or ""
            return _strip_thinking(text) or None
    except Exception as e:
        print(f"llm: {p} call failed, continuing heuristic-only: {e}")
    return None
