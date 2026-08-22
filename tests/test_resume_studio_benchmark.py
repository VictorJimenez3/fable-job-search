import time

from scripts import resume_studio_benchmark as benchmark


def _job(job_id, sector, company, score):
    return {
        "id": job_id,
        "sector": sector,
        "company": company,
        "title": "Software Engineer",
        "url": "https://example.com/" + job_id,
        "alert_ok": True,
        "posting_fetch": {"ok": True},
        "benchmark_match": {"score": score},
    }


def test_full_selection_round_robins_across_sectors_before_score():
    jobs = [
        _job("h1", "healthtech", "Health A", 100),
        _job("h2", "healthtech", "Health B", 99),
        _job("f1", "fintech", "Finance A", 70),
        _job("a1", "ai_lab", "AI A", 60),
        _job("o1", "other", "Other A", 50),
    ]
    selected = benchmark.select_full_jobs(jobs, 4)
    assert [item["id"] for item in selected] == ["h1", "a1", "f1", "o1"]


def test_full_selection_avoids_same_company_until_diversity_is_exhausted():
    jobs = [
        _job("a1", "ai_lab", "Same Co", 100),
        _job("a2", "ai_lab", "Same Co", 99),
        _job("f1", "fintech", "Other Co", 50),
    ]
    selected = benchmark.select_full_jobs(jobs, 3)
    assert [item["id"] for item in selected] == ["a1", "f1", "a2"]


def test_lab_summary_keeps_failures_and_panel_blockers_visible():
    summary = benchmark.summarize_lab_runs([
        {
            "ok": True,
            "summary": {
                "audit_readiness": "blocked",
                "audit_decision": "do_not_ship",
                "uplift_band": "negative",
                "critic_available": True,
                "critic_failed_roles": ["technical"],
                "finding_counts": {"BLOCKER": 2},
                "elapsed_seconds": 12.5,
            },
        },
        {
            "ok": False,
            "error": "final resume rejected: 0 wrap(s), 1 near-wrap(s)",
            "failure_class": "quality_rejection",
        },
    ])
    assert summary["runs"] == 2
    assert summary["successful_runs"] == 1
    assert summary["failed_runs"] == 1
    assert summary["quality_rejections"] == 1
    assert summary["execution_failures"] == 0
    assert summary["complete_critic_panels"] == 0
    assert summary["runs_with_blockers"] == 1
    assert summary["readiness"] == {"blocked": 1}


def test_benchmark_fresh_selection_excludes_terminal_and_old_roles():
    now = time.time()
    jobs = [
        {
            **_job("new", "healthtech", "New Co", 80),
            "posted_at": int(now - 2 * 86400),
            "posting_status": "open",
        },
        {
            **_job("old", "healthtech", "Old Co", 80),
            "posted_at": int(now - 10 * 86400),
            "posting_status": "open",
        },
        {
            **_job("closed", "healthtech", "Closed Co", 80),
            "posted_at": int(now - 1 * 86400),
            "posting_status": "expired",
            "closed_at": int(now - 100),
        },
    ]
    selected = benchmark.select_balanced_jobs(jobs, 8, fresh_days=7, now=now)
    assert [item["id"] for item in selected] == ["new"]


def test_benchmark_marks_definitive_closed_page_as_not_open():
    assert benchmark.DEFINITIVE_CLOSED_RE.search("This job has been closed.")
    assert not benchmark.DEFINITIVE_CLOSED_RE.search(
        "We build closed-loop systems and accept applications online."
    )


def test_benchmark_summary_preserves_model_effort_and_latency():
    result = benchmark.summarize_report({
        "tailoring_audit": {},
        "provider_flow": [{
            "label": "draft",
            "model": "gpt-5.6-luna",
            "reasoning_effort": "high",
            "elapsed_seconds": 12.4,
            "status": "complete",
        }],
        "usage": {"codex_calls": 1},
    })
    assert result["provider_flow"][0]["model"] == "gpt-5.6-luna"
    assert result["provider_flow"][0]["effort"] == "high"
    assert result["provider_flow"][0]["elapsed_seconds"] == 12.4
