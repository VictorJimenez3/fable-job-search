"""Bounded, provider-agnostic LLM access for Job Radar.

The radar is useful without AI.  This module adds AI only where it improves a
deterministic pipeline, and puts every hosted request behind three controls:

* task-specific model preferences;
* per-run request and task budgets; and
* retry, fallback, and cooldown for unreliable free endpoints.

Supported providers are local Ollama/OpenAI-compatible servers, four named
NVIDIA NIM models, a generic OpenAI-compatible endpoint, and Anthropic.  No
caller needs to know which provider answered and no secret is written to
telemetry.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
import time
from typing import Callable

import requests

from .config import env, profile

ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
NVIDIA_DEFAULT_BASE = "https://integrate.api.nvidia.com/v1"

_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 1
_EVENT_LIMIT = 200

_NVIDIA = {
    "glm": ("NVIDIA_GLM_52_API_KEY", "NVIDIA_GLM_52_MODEL"),
    "deepseek": ("NVIDIA_DEEPSEEK_V4_PRO_API_KEY", "NVIDIA_DEEPSEEK_V4_PRO_MODEL"),
    "nemotron": ("NVIDIA_NEMOTRON_3_ULTRA_550B_A55B_API_KEY",
                  "NVIDIA_NEMOTRON_3_ULTRA_550B_A55B_MODEL"),
    "kimi": ("NVIDIA_KIMI_K2_6_API_KEY", "NVIDIA_KIMI_K2_6_MODEL"),
}

# The first healthy model wins.  Kimi is valuable for research synthesis but
# is intentionally not first for operational grading: its NIM endpoint has
# shown intermittent model-unavailable responses.
_TASK_PREFERENCES = {
    "quality": ("glm", "deepseek", "nemotron", "kimi"),
    "pasted_jd": ("glm", "deepseek", "nemotron", "kimi"),
    # Production telemetry shows GLM is currently the only consistently valid
    # synthesis model. Keep DeepSeek as the useful fallback; Nemotron's JSON
    # completion and Kimi's endpoint are unreliable for this task.
    "company_research": ("glm", "deepseek", "nemotron", "kimi"),
    "rerank": ("glm", "deepseek", "nemotron", "kimi"),
    "discovery": ("nemotron", "glm", "deepseek", "kimi"),
    "strategy": ("nemotron", "glm", "deepseek", "kimi"),
    "general": ("glm", "deepseek", "nemotron", "kimi"),
}

_TASK_DEFAULT_LIMITS = {
    "quality": 18,
    "pasted_jd": 10,
    "company_research": 8,
    "rerank": 1,
    "discovery": 1,
    "strategy": 1,
    "general": 4,
}


@dataclass(frozen=True)
class Endpoint:
    name: str
    kind: str
    base_url: str
    model: str
    api_key: str = ""


class CallFailure(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class BudgetFailure(CallFailure):
    pass


_signature: tuple | None = None
_logical_calls = 0
_requests = 0
_task_calls: dict[str, int] = {}
_cooldowns: dict[str, float] = {}
_events: list[dict] = []


def _config_signature() -> tuple:
    names = ["ANTHROPIC_API_KEY", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL",
             "NVIDIA_API_BASE_URL", "RADAR_AI_MAX_CALLS", "RADAR_AI_MAX_REQUESTS",
             "RADAR_AI_PROVIDER_ATTEMPTS"]
    for key_name, model_name in _NVIDIA.values():
        names.extend((key_name, model_name))
    return tuple(os.environ.get(n, "") for n in names)


def reset_runtime() -> None:
    """Reset per-process budgets and health. Public mainly for tests/CLI runs."""
    global _signature, _logical_calls, _requests
    _signature = _config_signature()
    _logical_calls = 0
    _requests = 0
    _task_calls.clear()
    _cooldowns.clear()
    _events.clear()


def _ensure_runtime() -> None:
    if _signature != _config_signature():
        reset_runtime()


def _int_env(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(env(name, str(default))))
    except (TypeError, ValueError):
        return default


def _task_limit(task: str) -> int:
    default = _TASK_DEFAULT_LIMITS.get(task, _TASK_DEFAULT_LIMITS["general"])
    return _int_env(f"RADAR_AI_TASK_{task.upper()}_LIMIT", default)


def _ollama_api_url(base: str) -> str | None:
    """Return Ollama's native endpoint, which lets us unload after each call."""
    url = base.rstrip("/")
    if url in {"http://localhost:11434/v1", "http://127.0.0.1:11434/v1"}:
        return url[:-3] + "/api/chat"
    return None


def _is_local(base: str) -> bool:
    return base.startswith(("http://localhost", "http://127.0.0.1"))


def _endpoints(task: str) -> list[Endpoint]:
    result: list[Endpoint] = []
    compat_base = env("LLM_BASE_URL").rstrip("/")
    if compat_base and _is_local(compat_base):
        result.append(Endpoint("local", "compatible", compat_base,
                               env("LLM_MODEL") or profile()["llm"].get("local_model", "qwen3:14b"),
                               env("LLM_API_KEY")))

    nvidia_base = (env("NVIDIA_API_BASE_URL") or NVIDIA_DEFAULT_BASE).rstrip("/")
    for short in _TASK_PREFERENCES.get(task, _TASK_PREFERENCES["general"]):
        key_name, model_name = _NVIDIA[short]
        key, model = env(key_name), env(model_name)
        if key and model:
            result.append(Endpoint(f"nvidia:{short}", "compatible", nvidia_base, model, key))

    # Generic hosted compatible endpoints remain supported. Avoid duplicating a
    # local endpoint already added above.
    if compat_base and not _is_local(compat_base):
        result.append(Endpoint("compatible", "compatible", compat_base,
                               env("LLM_MODEL") or profile()["llm"].get("local_model", "qwen3:14b"),
                               env("LLM_API_KEY")))

    # Anthropic is last when free/local options exist, but remains the only
    # endpoint for older installations that configured just this key.
    if env("ANTHROPIC_API_KEY"):
        result.append(Endpoint("anthropic", "anthropic", ANTHROPIC_API,
                               profile()["llm"]["model"], env("ANTHROPIC_API_KEY")))

    # A generic endpoint and a named NIM can point at the same model/key. One
    # request is enough.
    unique, seen = [], set()
    for ep in result:
        marker = (ep.kind, ep.base_url, ep.model, ep.api_key[-8:])
        if marker not in seen:
            unique.append(ep)
            seen.add(marker)
    return unique


def provider() -> str | None:
    eps = _endpoints("general")
    if not eps:
        return None
    has_local = any(_is_local(ep.base_url) for ep in eps)
    has_api = any(not _is_local(ep.base_url) for ep in eps)
    if has_local and has_api:
        return "local + api-router"
    if any(ep.name.startswith("nvidia:") for ep in eps):
        return "nvidia-router"
    return eps[0].name


def available(task: str = "general") -> bool:
    return bool(_endpoints(task))


def _post_with_retry(url: str, *, timeout: int, json: dict,
                     headers: dict | None = None):
    """POST with bounded Retry-After-aware retry for transient free-tier errors."""
    _reserve_request()
    response = requests.post(url, timeout=timeout, json=json, headers=headers)
    for _ in range(_MAX_RETRIES):
        if response.status_code not in _RETRY_STATUSES:
            break
        try:
            wait = min(8, int(float(response.headers.get("Retry-After") or 3)))
        except (TypeError, ValueError):
            wait = 3
        time.sleep(max(1, wait))
        _reserve_request()
        response = requests.post(url, timeout=timeout, json=json, headers=headers)
    return response


def _reserve_request() -> None:
    """Count the actual HTTP send, including transport retries."""
    global _requests
    maximum = _int_env("RADAR_AI_MAX_REQUESTS", 24)
    if _requests >= maximum:
        raise BudgetFailure("per-run provider request budget reached")
    _requests += 1


def _strip_thinking(text: str) -> str:
    if "</think>" in text:
        text = text.split("</think>")[-1]
    return text.strip()


def _response_error(response) -> None:
    status = getattr(response, "status_code", 200)
    if status >= 400:
        try:
            detail = str(response.json())[:240]
        except Exception:
            detail = str(getattr(response, "text", ""))[:240]
        raise CallFailure(f"HTTP {status}: {detail}", status)


def _call(ep: Endpoint, prompt: str, max_tokens: int, timeout: int,
          json_mode: bool) -> tuple[str | None, dict]:
    if ep.kind == "anthropic":
        response = _post_with_retry(ep.base_url, timeout=timeout, json={
            "model": ep.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }, headers={"x-api-key": ep.api_key, "anthropic-version": "2023-06-01"})
        _response_error(response)
        body = response.json()
        text = "".join(b.get("text", "") for b in body.get("content", []))
        return _strip_thinking(text) or None, body.get("usage") or {}

    ollama_url = _ollama_api_url(ep.base_url)
    if ollama_url:
        payload = {
            "model": ep.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "think": False,
            "keep_alive": 0,
            "options": {"num_predict": max_tokens},
        }
        if json_mode:
            payload["format"] = "json"
        _reserve_request()
        response = requests.post(ollama_url, timeout=timeout, json=payload)
        _response_error(response)
        body = response.json()
        text = (body.get("message") or {}).get("content") or ""
        usage = {"prompt_tokens": body.get("prompt_eval_count", 0),
                 "completion_tokens": body.get("eval_count", 0)}
        return _strip_thinking(text) or None, usage

    headers = {"Content-Type": "application/json"}
    if ep.api_key:
        headers["Authorization"] = f"Bearer {ep.api_key}"
    response = _post_with_retry(f"{ep.base_url}/chat/completions", timeout=timeout, json={
        "model": ep.model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }, headers=headers)
    _response_error(response)
    body = response.json()
    choices = body.get("choices") or []
    if not choices:
        return None, body.get("usage") or {}
    message = choices[0].get("message") or {}
    text = message.get("content") or message.get("reasoning_content") or ""
    return _strip_thinking(text) or None, body.get("usage") or {}


def _record(task: str, ep: Endpoint | None, status: str, started: float,
            prompt: str, max_tokens: int, usage: dict | None = None,
            detail: str = "") -> dict:
    usage = usage or {}
    event = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "task": task,
        "endpoint": ep.name if ep else "none",
        "lane": ("local" if ep and _is_local(ep.base_url) else
                  "api" if ep else "router"),
        "model": ep.model if ep else "",
        "status": status,
        "latency_ms": round((time.monotonic() - started) * 1000),
        "prompt_chars": len(prompt),
        "max_tokens": max_tokens,
        "prompt_tokens": usage.get("prompt_tokens") or usage.get("input_tokens") or 0,
        "completion_tokens": usage.get("completion_tokens") or usage.get("output_tokens") or 0,
        "detail": detail[:120],
    }
    _events.append(event)
    del _events[:-_EVENT_LIMIT]
    return event


def _cooldown(ep: Endpoint, status: int | None) -> None:
    seconds = 60
    if status in {401, 403, 404}:
        seconds = 30 * 60
    elif status == 429:
        seconds = 2 * 60
    _cooldowns[ep.name] = time.monotonic() + seconds


def complete(prompt: str, max_tokens: int = 2000, timeout: int = 180,
             json_mode: bool = False, task: str = "general",
             validator: Callable[[str], bool] | None = None) -> str | None:
    """Return one completion, or ``None`` so the caller uses its heuristic.

    ``task`` selects a model order and a per-run cap. The caps are intentionally
    conservative because the configured NIM keys are free-tier and have no
    attached billing method.
    """
    global _logical_calls
    _ensure_runtime()
    task = task if task in _TASK_PREFERENCES else "general"
    eps = _endpoints(task)
    if not eps:
        return None

    # Hosted providers can have very different latency and schema reliability.
    # When enabled, spread independent calls across configured API models so a
    # flaky first-choice model does not serialize the entire backlog behind its
    # timeout. Fallback still runs if the rotated provider fails validation.
    if env("RADAR_AI_ROTATE_PROVIDERS") == "1" and len(eps) > 1:
        local = [ep for ep in eps if _is_local(ep.base_url)]
        hosted = [ep for ep in eps if not _is_local(ep.base_url)]
        if hosted:
            offset = _logical_calls % len(hosted)
            hosted = hosted[offset:] + hosted[:offset]
            eps = local + hosted

    max_calls = _int_env("RADAR_AI_MAX_CALLS", 12)
    max_requests = _int_env("RADAR_AI_MAX_REQUESTS", 24)
    if (_logical_calls >= max_calls or _task_calls.get(task, 0) >= _task_limit(task)
            or _requests >= max_requests):
        _record(task, None, "budget_skipped", time.monotonic(), prompt, max_tokens,
                detail="per-run AI budget reached")
        return None

    _logical_calls += 1
    _task_calls[task] = _task_calls.get(task, 0) + 1
    provider_attempts = _int_env("RADAR_AI_PROVIDER_ATTEMPTS", 2, minimum=1)
    # Local Ollama and hosted/API are independent health checks. Return the
    # first valid answer, but probe both lanes so outages are visible.
    lanes = {"local" if _is_local(ep.base_url) else "api" for ep in eps}
    lane_success: set[str] = set()
    api_attempts = 0
    selected: dict | None = None
    for ep in eps:
        lane = "local" if _is_local(ep.base_url) else "api"
        if lane in lane_success or _requests >= max_requests:
            break
        if lane == "api" and api_attempts >= provider_attempts:
            continue
        if _cooldowns.get(ep.name, 0) > time.monotonic():
            continue
        if lane == "api":
            api_attempts += 1
        started = time.monotonic()
        try:
            text, usage = _call(ep, prompt, max_tokens, timeout, json_mode)
            if text and validator is not None and not validator(text):
                _record(task, ep, "invalid", started, prompt, max_tokens, usage,
                        detail="task schema validation failed")
                _cooldown(ep, None)
                continue
            if text:
                event = _record(task, ep, "ok", started, prompt, max_tokens, usage)
                lane_success.add(lane)
                if selected is None:
                    selected = {"text": text, "event": event}
                if lane_success >= lanes:
                    break
                continue
            _record(task, ep, "empty", started, prompt, max_tokens, usage)
            _cooldown(ep, None)
        except Exception as exc:
            status = exc.status if isinstance(exc, CallFailure) else None
            _record(task, ep, "error", started, prompt, max_tokens,
                    detail=f"{type(exc).__name__}: {exc}")
            _cooldown(ep, status)
            print(f"llm: {task} via {ep.name} failed; trying fallback: {exc}")
    if selected:
        selected["event"]["selected"] = True
        return selected["text"]
    return None


def usage_report() -> dict:
    """Return bounded, secret-free telemetry suitable for committing to state."""
    _ensure_runtime()
    models: dict[str, dict] = {}
    for event in _events:
        name = event["endpoint"]
        if name == "none":
            continue
        row = models.setdefault(name, {"model": event["model"], "requests": 0,
                                       "ok": 0, "errors": 0, "tokens": 0})
        row["requests"] += 1
        row["ok"] += event["status"] == "ok"
        row["errors"] += event["status"] in {"error", "empty", "invalid"}
        row["tokens"] += event["prompt_tokens"] + event["completion_tokens"]
    return {
        "generated_at": int(time.time()),
        "provider": provider(),
        "limits": {"logical_calls": _int_env("RADAR_AI_MAX_CALLS", 12),
                   "requests": _int_env("RADAR_AI_MAX_REQUESTS", 24)},
        "logical_calls": _logical_calls,
        "requests": _requests,
        "task_calls": dict(sorted(_task_calls.items())),
        "models": models,
        "events": list(_events[-40:]),
    }


def save_usage() -> dict:
    """Persist the current run's telemetry for the platform and diagnostics."""
    report = usage_report()
    from . import state
    previous = state.load("ai_usage.json", {})
    history = list(previous.get("history") or [])
    if report["logical_calls"] or report["events"]:
        history.append({k: report[k] for k in ("generated_at", "provider", "logical_calls",
                                               "requests", "task_calls", "models")})
        history[-1]["attempts"] = report["events"]
    report["history"] = history[-30:]
    state.save("ai_usage.json", report)
    return report
