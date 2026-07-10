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
            # some local models wrap reasoning in <think> blocks; strip them
            if "</think>" in text:
                text = text.split("</think>")[-1]
            return text.strip() or None
    except Exception as e:
        print(f"llm: {p} call failed, continuing heuristic-only: {e}")
    return None
