#!/usr/bin/env python3
"""Quality-first Resume Studio benchmark runner.

The benchmark deliberately separates cheap corpus coverage from expensive AI
tailoring.  ``--fetch-only``/``--match-only`` can cover dozens of live jobs in
parallel; ``--run-full`` then runs a smaller, balanced set through the real
Luna Max writer plus sealed evaluator.  Every run stays below ignored
``CV/.resume_studio/benchmarks/`` and records latency, gate outcomes, panel
completeness, and comparative uplift.

Examples::

    .venv/bin/python scripts/resume_studio_benchmark.py \
        --limit 48 --fetch-workers 12 --fetch-only
    .venv/bin/python scripts/resume_studio_benchmark.py \
        --manifest CV/.resume_studio/benchmarks/<id>/manifest.json \
        --run-full --full-limit 8 --workers 2
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from scripts import resume_studio as rs


ROLE_RE = re.compile(
    r"\b(software|machine learning|ml engineer|data scientist|data engineer|"
    r"ai engineer|research engineer|backend|full[ -]?stack|frontend|"
    r"systems engineer|platform|developer)\b",
    re.I,
)
EXCLUDE_TITLE_RE = re.compile(
    r"\b(senior|sr\.?|staff|principal|director|manager|vice president|vp|"
    r"head of|intern|internship|co[- ]?op|apprentice)\b",
    re.I,
)
SECTOR_ORDER = (
    "healthtech", "big_tech", "ai_lab", "fintech", "sports", "video_games", "edtech", "other",
)
SECTOR_QUOTAS = {
    "healthtech": 8,
    "big_tech": 8,
    "ai_lab": 6,
    "fintech": 5,
    "sports": 3,
    "video_games": 3,
    "edtech": 3,
    "other": 12,
}


def now_stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def load_jobs(root: Path) -> List[Dict[str, Any]]:
    value = rs.read_json(root / "state/jobs.json", {}) or {}
    jobs = list(value.values()) if isinstance(value, dict) else list(value)
    return [item for item in jobs if isinstance(item, dict)]


def job_eligible_for_benchmark(job: Dict[str, Any]) -> bool:
    title = str(job.get("title") or "")
    url = str(job.get("url") or "")
    return bool(
        url.startswith(("http://", "https://"))
        and ROLE_RE.search(title)
        and not EXCLUDE_TITLE_RE.search(title)
        and (job.get("alert_ok") or job.get("early_career_possible"))
    )


def _stable_key(job: Dict[str, Any]) -> Tuple[Any, ...]:
    return (
        -int(bool(job.get("alert_ok"))),
        -int(bool(job.get("early_career_possible"))),
        -int(job.get("score") or 0),
        -int(job.get("posted_at") or 0),
        str(job.get("company") or ""),
        str(job.get("title") or ""),
    )


def select_balanced_jobs(jobs: Iterable[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    candidates = [job for job in jobs if job_eligible_for_benchmark(job)]
    grouped: Dict[str, List[Dict[str, Any]]] = {sector: [] for sector in SECTOR_ORDER}
    for job in candidates:
        grouped.setdefault(str(job.get("sector") or "other"), []).append(job)
    for values in grouped.values():
        values.sort(key=_stable_key)
    selected: List[Dict[str, Any]] = []
    seen_companies = set()
    # First pass maximizes company and sector diversity. A second pass fills
    # the requested count after every sector has had a chance.
    for sector in SECTOR_ORDER:
        quota = min(SECTOR_QUOTAS.get(sector, 2), max(0, limit - len(selected)))
        for job in grouped.get(sector, []):
            company = str(job.get("company") or "").strip().lower()
            if company in seen_companies:
                continue
            selected.append(dict(job))
            seen_companies.add(company)
            if len(selected) >= limit or sum(1 for item in selected if str(item.get("sector") or "other") == sector) >= quota:
                break
        if len(selected) >= limit:
            break
    if len(selected) < limit:
        for job in sorted(candidates, key=_stable_key):
            identity = str(job.get("id") or job.get("url") or "")
            if any(str(item.get("id") or item.get("url") or "") == identity for item in selected):
                continue
            selected.append(dict(job))
            if len(selected) >= limit:
                break
    return selected[:limit]


def _fetch_one(job: Dict[str, Any]) -> Dict[str, Any]:
    started = time.time()
    result = dict(job)
    try:
        text = rs.fetch_job_description(job)
        result["posting_text"] = text
        result["posting_fetch"] = {
            "ok": len(text) >= 300,
            "chars": len(text),
            "elapsed_seconds": round(time.time() - started, 2),
        }
    except Exception as exc:  # noqa: BLE001 - benchmark records source failures
        result["posting_text"] = ""
        result["posting_fetch"] = {
            "ok": False,
            "chars": 0,
            "elapsed_seconds": round(time.time() - started, 2),
            "error": str(exc),
        }
    return result


def fetch_postings(jobs: List[Dict[str, Any]], workers: int) -> List[Dict[str, Any]]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(_fetch_one, job) for job in jobs]
        return [future.result() for future in futures]


def _match_one(job: Dict[str, Any]) -> Dict[str, Any]:
    started = time.time()
    result = dict(job)
    try:
        match = rs.resume_match_for_job(
            job, rs.repo_root(), posting_text=str(job.get("posting_text") or ""),
        )
        result["benchmark_match"] = match
        result["benchmark_match_elapsed_seconds"] = round(time.time() - started, 2)
    except Exception as exc:  # noqa: BLE001 - benchmark records analysis failures
        result["benchmark_match"] = {"error": str(exc), "score": None, "confidence": "error"}
        result["benchmark_match_elapsed_seconds"] = round(time.time() - started, 2)
    return result


def match_postings(jobs: List[Dict[str, Any]], workers: int) -> List[Dict[str, Any]]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(_match_one, job) for job in jobs]
        return [future.result() for future in futures]


def _match_score(job: Dict[str, Any]) -> float:
    value = (job.get("benchmark_match") or {}).get("score")
    try:
        return float(value)
    except (TypeError, ValueError):
        return -1.0


def select_full_jobs(jobs: Iterable[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """Choose expensive runs by sector/company diversity before raw score.

    A benchmark that only runs the highest matching postings can look healthy
    while quietly testing one role family.  The first pass is round-robin by
    sector and only admits one company per pass; the fill pass then uses match
    quality while preserving as much company diversity as the corpus permits.
    """
    candidates = [
        dict(job) for job in jobs
        if (job.get("posting_fetch") or {}).get("ok") and _match_score(job) >= 0
    ]
    grouped: Dict[str, List[Dict[str, Any]]] = {sector: [] for sector in SECTOR_ORDER}
    for job in candidates:
        grouped.setdefault(str(job.get("sector") or "other"), []).append(job)
    for values in grouped.values():
        values.sort(key=lambda item: (-_match_score(item), _stable_key(item)))

    selected: List[Dict[str, Any]] = []
    selected_ids = set()
    seen_companies = set()
    cursors = {sector: 0 for sector in grouped}
    target = max(0, int(limit))
    while len(selected) < target:
        progress = False
        for sector in SECTOR_ORDER:
            values = grouped.get(sector, [])
            while cursors.get(sector, 0) < len(values):
                job = values[cursors[sector]]
                cursors[sector] += 1
                identity = str(job.get("id") or job.get("url") or "")
                company = str(job.get("company") or "").strip().lower()
                if identity in selected_ids or company in seen_companies:
                    continue
                selected.append(job)
                selected_ids.add(identity)
                seen_companies.add(company)
                progress = True
                break
            if len(selected) >= target:
                break
        if not progress:
            break

    if len(selected) < target:
        for job in sorted(candidates, key=lambda item: (-_match_score(item), _stable_key(item))):
            identity = str(job.get("id") or job.get("url") or "")
            if identity in selected_ids:
                continue
            selected.append(job)
            selected_ids.add(identity)
            if len(selected) >= target:
                break
    return selected[:target]


def _run_one(
    job: Dict[str, Any], root: Path, benchmark_root: Path, mode: str,
    quality_profile: str = rs.QUALITY_PROFILE_DEFAULT,
) -> Dict[str, Any]:
    quality_profile = rs.normalize_quality_profile(quality_profile)
    run_id = uuid.uuid4().hex[:12]
    run_dir = benchmark_root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    job = dict(job)
    job["_resume_studio_run_id"] = run_id
    status = {
        "run_id": run_id,
        "status": "running",
        "mode": mode,
        "quality_profile": quality_profile,
        "job": rs.job_summary(job),
        "started_at": rs.now_iso(),
    }
    rs.write_json(run_dir / "status.json", status)
    rs.write_json(run_dir / "job.json", rs.job_summary(job))

    def update(step: str, message: str, report: Optional[Dict[str, Any]] = None) -> None:
        current = rs.read_json(run_dir / "status.json", {}) or status
        current.update({"status": "running", "step": step, "message": message, "updated_at": rs.now_iso()})
        if report is not None:
            current["report"] = report
        rs.write_json(run_dir / "status.json", current)

    started = time.time()
    try:
        if mode == "ai":
            rs.run_tailoring(run_dir, job, update, enhance=True, quality_profile=quality_profile)
        elif mode == "unrestricted":
            rs.run_tailoring(
                run_dir, job, update, enhance=True, unrestricted=True,
                quality_profile=quality_profile,
            )
        elif mode == "generation":
            rs.run_tailoring(
                run_dir, job, update, enhance=True, unrestricted=True, generation=True,
                quality_profile=quality_profile,
            )
        else:
            rs.run_tailoring(run_dir, job, update, enhance=False, quality_profile=quality_profile)
        report = rs.read_json(run_dir / "report.json", {}) or {}
        finished_at = rs.now_iso()
        terminal = rs.read_json(run_dir / "status.json", {}) or status
        terminal.update({
            "status": "complete",
            "step": "complete",
            "message": "Benchmark run completed",
            "finished_at": finished_at,
            "updated_at": finished_at,
            "report": report,
        })
        rs.write_json(run_dir / "status.json", terminal)
        return {
            "run_id": run_id,
            "ok": True,
            "mode": mode,
            "quality_profile": quality_profile,
            "job_id": job.get("id"),
            "company": job.get("company"),
            "title": job.get("title"),
            "sector": job.get("sector") or "other",
            "match_score": _match_score(job),
            "run_dir": str(run_dir),
            "elapsed_seconds": round(time.time() - started, 1),
            "summary": summarize_report(report),
        }
    except Exception as exc:  # noqa: BLE001 - benchmark must continue
        error = str(exc)
        quality_rejected = bool(
            (run_dir / "layout_rejection.json").exists()
            or error.startswith("final resume rejected:")
        )
        failure_class = "quality_rejection" if quality_rejected else "execution_failure"
        rs.write_json(
            run_dir / "benchmark_error.json",
            {"error": error, "failure_class": failure_class},
        )
        finished_at = rs.now_iso()
        terminal = rs.read_json(run_dir / "status.json", {}) or status
        terminal.update({
            "status": "failed",
            "step": "failed",
            "message": error[:300],
            "error": error,
            "failure_class": failure_class,
            "finished_at": finished_at,
            "updated_at": finished_at,
        })
        rs.write_json(run_dir / "status.json", terminal)
        return {
            "run_id": run_id,
            "ok": False,
            "mode": mode,
            "quality_profile": quality_profile,
            "job_id": job.get("id"),
            "company": job.get("company"),
            "title": job.get("title"),
            "sector": job.get("sector") or "other",
            "match_score": _match_score(job),
            "run_dir": str(run_dir),
            "elapsed_seconds": round(time.time() - started, 1),
            "error": error,
            "failure_class": failure_class,
        }


def summarize_report(report: Dict[str, Any]) -> Dict[str, Any]:
    audit = report.get("tailoring_audit") or {}
    review = report.get("review") or {}
    panel = report.get("review_panel") or report.get("critic_panel") or {}
    comparison = audit.get("comparison") or {}
    return {
        "approval_state": report.get("approval_state"),
        "review_ready": review.get("ready"),
        "review_hard_fail": review.get("hard_fail"),
        "audit_status": audit.get("status"),
        "audit_decision": audit.get("decision"),
        "audit_readiness": audit.get("readiness"),
        "uplift_band": comparison.get("uplift_band"),
        "gain_weight": comparison.get("gain_weight"),
        "loss_weight": comparison.get("loss_weight"),
        "finding_counts": audit.get("finding_counts"),
        "critic_available": panel.get("available"),
        "critic_roles": panel.get("roles"),
        "critic_failed_roles": panel.get("failed_roles"),
        "critic_contract": panel.get("contract_version"),
        "quality_profile": report.get("quality_profile"),
        "elapsed_seconds": (report.get("run_metrics") or {}).get("elapsed_seconds"),
        "codex_calls": (report.get("usage") or {}).get("codex_calls"),
    }


def summarize_lab_runs(runs: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    values = [item for item in runs if isinstance(item, dict)]
    summaries = [item.get("summary") or {} for item in values if item.get("ok")]

    def counts(field: str) -> Dict[str, int]:
        result: Dict[str, int] = {}
        for summary in summaries:
            value = str(summary.get(field) or "unknown")
            result[value] = result.get(value, 0) + 1
        return dict(sorted(result.items()))

    return {
        "runs": len(values),
        "successful_runs": sum(bool(item.get("ok")) for item in values),
        "failed_runs": sum(not bool(item.get("ok")) for item in values),
        "quality_rejections": sum(
            item.get("failure_class") == "quality_rejection" for item in values
        ),
        "execution_failures": sum(
            (not bool(item.get("ok")))
            and item.get("failure_class") != "quality_rejection"
            for item in values
        ),
        "readiness": counts("audit_readiness"),
        "tailoring": counts("audit_decision"),
        "uplift_bands": counts("uplift_band"),
        "complete_critic_panels": sum(
            bool(summary.get("critic_available"))
            and not bool(summary.get("critic_failed_roles"))
            for summary in summaries
        ),
        "runs_with_blockers": sum(
            bool((summary.get("finding_counts") or {}).get("BLOCKER"))
            for summary in summaries
        ),
        "elapsed_seconds": [
            summary.get("elapsed_seconds") for summary in summaries
            if isinstance(summary.get("elapsed_seconds"), (int, float))
        ],
    }


def run_full(
    jobs: List[Dict[str, Any]], root: Path, benchmark_root: Path, mode: str,
    workers: int, limit: int, on_result: Optional[Any] = None,
    quality_profile: str = rs.QUALITY_PROFILE_DEFAULT,
) -> List[Dict[str, Any]]:
    """Run the expensive cohort concurrently and checkpoint each terminal run."""
    selected = select_full_jobs(jobs, max(1, limit))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(_run_one, job, root, benchmark_root, mode, quality_profile): index
            for index, job in enumerate(selected)
        }
        completed: List[Dict[str, Any]] = []
        by_index: Dict[int, Dict[str, Any]] = {}
        for future in concurrent.futures.as_completed(futures):
            index = futures[future]
            result = future.result()
            by_index[index] = result
            completed.append(result)
            if on_result is not None:
                on_result(result, list(completed), selected)
        return [by_index[index] for index in range(len(selected))]


def manifest_summary(manifest: Dict[str, Any]) -> Dict[str, Any]:
    """Return compact progress fields safe to checkpoint during a long lab."""
    jobs = manifest.get("jobs") or []
    full_runs = manifest.get("full_runs") or []
    return {
        "jobs_selected": len(jobs),
        "jobs_with_posting": sum(bool((item.get("posting_fetch") or {}).get("ok")) for item in jobs),
        "jobs_with_match": sum(bool((item.get("benchmark_match") or {}).get("score") is not None) for item in jobs),
        "full_runs": len(full_runs),
        "full_successes": sum(bool(item.get("ok")) for item in full_runs),
    }


def write_manifest(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rs.write_json(path, payload)


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--limit", type=int, default=48)
    parser.add_argument("--fetch-workers", type=int, default=12)
    parser.add_argument("--match-workers", type=int, default=8)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--full-limit", type=int, default=8)
    parser.add_argument("--mode", choices=["used", "ai", "unrestricted", "generation"], default="ai")
    parser.add_argument(
        "--quality-profile", choices=sorted(rs.QUALITY_PROFILES),
        default=rs.QUALITY_PROFILE_DEFAULT,
        help="Authoring latency/quality lane; the sealed evaluator remains the same.",
    )
    parser.add_argument("--fetch-only", action="store_true")
    parser.add_argument("--match-only", action="store_true")
    parser.add_argument("--run-full", action="store_true")
    args = parser.parse_args(argv)
    root = rs.repo_root()
    if args.manifest:
        manifest_path = args.manifest.expanduser().resolve()
        manifest = rs.read_json(manifest_path, {}) or {}
        benchmark_root = manifest_path.parent
    else:
        benchmark_root = root / "CV" / ".resume_studio" / "benchmarks" / (now_stamp() + "-" + uuid.uuid4().hex[:6])
        manifest_path = benchmark_root / "manifest.json"
        manifest = {
            "benchmark_version": "resume-studio-benchmark-v1",
            "created_at": rs.now_iso(),
            "selection": {"limit": args.limit, "sector_quotas": SECTOR_QUOTAS},
            "source": "state/jobs.json",
            "quality_profile": rs.normalize_quality_profile(args.quality_profile),
            "evaluator_contract": {
                "version": rs.SEALED_EVALUATOR_CONTRACT,
                "fingerprint": rs.resume_evaluator.contract_fingerprint(),
                "rubric_sha256": rs.resume_evaluator.EVALUATOR_RUBRIC_SHA256,
            },
        }
        selected = select_balanced_jobs(load_jobs(root), args.limit)
        manifest["jobs"] = selected
    manifest["benchmark_version"] = "resume-studio-benchmark-v2"
    manifest["quality_profile"] = rs.normalize_quality_profile(
        manifest.get("quality_profile") or args.quality_profile
    )
    manifest["status"] = "running" if args.run_full else "prepared"
    manifest["updated_at"] = rs.now_iso()
    manifest["evaluator_contract"] = {
        "version": rs.SEALED_EVALUATOR_CONTRACT,
        "fingerprint": rs.resume_evaluator.contract_fingerprint(),
        "rubric_sha256": rs.resume_evaluator.EVALUATOR_RUBRIC_SHA256,
    }
    jobs_need_fetch = any(
        not bool((item.get("posting_fetch") or {}).get("ok"))
        for item in manifest.get("jobs") or []
    )
    if jobs_need_fetch:
        manifest["jobs"] = fetch_postings(manifest.get("jobs") or [], args.fetch_workers)
    if args.fetch_only or not manifest.get("jobs"):
        manifest["jobs"] = [item for item in manifest.get("jobs") or [] if (item.get("posting_fetch") or {}).get("ok")]
    if args.match_only or args.run_full:
        manifest["jobs"] = match_postings(manifest.get("jobs") or [], args.match_workers)
    if args.run_full:
        manifest["full_selection"] = [
            {
                "job_id": item.get("id"),
                "company": item.get("company"),
                "title": item.get("title"),
                "sector": item.get("sector") or "other",
                "match_score": _match_score(item),
            }
            for item in select_full_jobs(manifest.get("jobs") or [], max(1, args.full_limit))
        ]
        manifest["full_runs"] = []

        def checkpoint(result: Dict[str, Any], completed: List[Dict[str, Any]], selected: List[Dict[str, Any]]) -> None:
            manifest["full_runs"] = completed
            manifest["full_progress"] = {
                "completed": len(completed), "selected": len(selected),
                "last_run_id": result.get("run_id"), "last_company": result.get("company"),
            }
            manifest["quality_summary"] = summarize_lab_runs(completed)
            manifest["summary"] = manifest_summary(manifest)
            manifest["updated_at"] = rs.now_iso()
            write_manifest(manifest_path, manifest)

        manifest["summary"] = manifest_summary(manifest)
        write_manifest(manifest_path, manifest)
        manifest["full_runs"] = run_full(
            manifest.get("jobs") or [], root, benchmark_root, args.mode, args.workers,
            args.full_limit, on_result=checkpoint,
            quality_profile=manifest["quality_profile"],
        )
        manifest["quality_summary"] = summarize_lab_runs(manifest["full_runs"])
        manifest["status"] = "complete"
    manifest["summary"] = manifest_summary(manifest)
    manifest["updated_at"] = rs.now_iso()
    write_manifest(manifest_path, manifest)
    print(json.dumps({"manifest": str(manifest_path), "summary": manifest["summary"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
