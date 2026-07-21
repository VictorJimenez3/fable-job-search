"""Measure configured hosted models on the radar's real structured tasks.

This is intentionally separate from the runtime race: every provider receives
the same prompt, and the report records validity and latency per task without
recording prompts, responses, or credentials.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from radar import company_research, quality, state


MODELS = (
    ("glm", "NVIDIA_GLM_52_API_KEY", "NVIDIA_GLM_52_MODEL"),
    ("deepseek", "NVIDIA_DEEPSEEK_V4_PRO_API_KEY", "NVIDIA_DEEPSEEK_V4_PRO_MODEL"),
    ("nemotron", "NVIDIA_NEMOTRON_3_ULTRA_550B_A55B_API_KEY", "NVIDIA_NEMOTRON_3_ULTRA_550B_A55B_MODEL"),
    ("kimi", "NVIDIA_KIMI_K2_6_API_KEY", "NVIDIA_KIMI_K2_6_MODEL"),
)


def prompts() -> dict[str, tuple[str, callable]]:
    record = {
        "name": "Acme Health",
        "sources": [{
            "id": "src1", "title": "About Acme Health", "retrieved_at": int(time.time()),
            "url": "https://example.com/about",
            "excerpt": "Acme Health makes software that helps hospitals coordinate patient care."
        }, {
            "id": "src2", "title": "Acme Health careers", "retrieved_at": int(time.time()),
            "url": "https://example.com/careers",
            "excerpt": "Our data teams build tools for clinicians and work with healthcare data."
        }],
    }
    company_prompt = company_research._prompt(record)
    quality_prompt = quality.PROMPT.format(
        title="Software Engineer, New Grad", company="Acme Health",
        text=("Acme Health is hiring a new graduate software engineer. "
               "The role builds Python services and requires a bachelor's degree."))
    return {
        "company_research": (company_prompt,
                              lambda text: company_research.parse_synthesis(text, {"src1", "src2"}) is not None),
        "quality": (quality_prompt, lambda text: quality._parse_verdict(text) is not None),
    }


def one(label: str, key: str, model: str, task: str,
        prompt: str, validator) -> dict:
    started = time.monotonic()
    result = {"provider": label, "model": model, "task": task,
              "status": "error", "valid": False, "latency_ms": 0}
    try:
        response = requests.post(
            os.environ["NVIDIA_BASE_URL"].rstrip("/") + "/chat/completions",
            timeout=int(os.environ.get("BENCHMARK_TIMEOUT", "90")),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "max_tokens": 2200 if task == "company_research" else 240,
                  "messages": [{"role": "user", "content": prompt}]},
        )
        result["http_status"] = response.status_code
        if response.status_code >= 400:
            result["status"] = "http_error"
        else:
            body = response.json()
            text = ((body.get("choices") or [{}])[0].get("message") or {}).get("content", "")
            result["status"] = "valid" if validator(text) else "invalid_schema"
            result["valid"] = result["status"] == "valid"
    except Exception as exc:
        result["status"] = type(exc).__name__
    result["latency_ms"] = round((time.monotonic() - started) * 1000)
    return result


def main() -> int:
    tasks = prompts()
    jobs = []
    for label, key_name, model_name in MODELS:
        key, model = os.environ.get(key_name, ""), os.environ.get(model_name, "")
        if not key or not model:
            for task in tasks:
                jobs.append({"provider": label, "model": model or "", "task": task,
                             "status": "not_configured", "valid": False, "latency_ms": 0})
            continue
        for task, (prompt, validator) in tasks.items():
            jobs.append((label, key, model, task, prompt, validator))

    results = []
    with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as pool:
        futures = [pool.submit(one, *job) for job in jobs if isinstance(job, tuple)]
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda row: (row["task"], row["provider"]))
    winners = {}
    for task in tasks:
        valid = [r for r in results if r["task"] == task and r["valid"]]
        winners[task] = min(valid, key=lambda r: r["latency_ms"])["provider"] if valid else None
    report = {"generated_at": int(time.time()), "tasks": results, "winners": winners}
    state.save("ai_benchmark.json", report)
    print(json.dumps(report, indent=2))
    return 0 if any(winners.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
