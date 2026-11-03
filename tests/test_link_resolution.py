import time
from types import SimpleNamespace

import radar.main as radar_main
from radar import link_resolver
from radar.alerts import format_line
from radar.dedupe import collapse_cross_source_jobs
from radar.digest import render_dashboard
from radar.main import _merge_job_sighting, _merge_record_sighting, _unique_discovered_jobs
from radar.models import Job
from radar.score import gates


def _response(url, text="", status_code=200):
    return SimpleNamespace(url=url, text=text, status_code=status_code)


def test_resolver_promotes_explicit_ats_link(monkeypatch):
    aggregator = "https://jobright.ai/jobs/info/abc123"
    direct = "https://jobs.lever.co/acme/role-123"
    monkeypatch.setattr(
        link_resolver.http, "get",
        lambda url, **kwargs: _response(aggregator, f'<a href="{direct}">Apply now</a>'),
    )
    result = link_resolver.resolve_link(aggregator, now=100)
    assert result["status"] == "resolved"
    assert result["resolved_url"] == direct
    assert result["ats"] == "lever"


def test_resolver_marks_jobright_closed_banner_as_expired(monkeypatch):
    aggregator = "https://jobright.ai/jobs/info/abc123"
    direct = "https://jobs.lever.co/acme/role-123"
    monkeypatch.setattr(
        link_resolver.http, "get",
        lambda url, **kwargs: _response(
            aggregator,
            f'<div class="expired">This job has closed.</div><a href="{direct}">Apply</a>',
        ),
    )
    monkeypatch.setattr(
        link_resolver, "_public_jobright_detail",
        lambda url: (_ for _ in ()).throw(AssertionError("closed pages need no detail lookup")),
    )

    result = link_resolver.resolve_link(aggregator, now=100)

    assert result["status"] == "closed"
    assert result["posting_status"] == "expired"
    assert result["page_signal_version"] == link_resolver.JOBRIGHT_PAGE_SIGNAL_VERSION
    assert "job has closed" in result["reason"].lower()


def test_resolver_retries_jobright_tracking_url_without_query(monkeypatch):
    tracking = "https://jobright.ai/jobs/info/abc123?utm_source=board"
    canonical = "https://jobright.ai/jobs/info/abc123"
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        body = "This job has closed." if url == canonical else "<html>visitor variant</html>"
        return _response(url, body)

    monkeypatch.setattr(link_resolver.http, "get", fake_get)
    monkeypatch.setattr(
        link_resolver, "_public_jobright_detail",
        lambda url: (_ for _ in ()).throw(AssertionError("closed canonical page needs no detail lookup")),
    )

    result = link_resolver.resolve_link(tracking, now=100)

    assert result["status"] == "closed"
    assert result["page_signal_version"] == link_resolver.JOBRIGHT_PAGE_SIGNAL_VERSION
    assert calls == [tracking, canonical]


def test_resolver_accepts_explicit_company_application_anchor(monkeypatch):
    aggregator = "https://jobright.ai/jobs/info/abc123"
    direct = "https://careers.acme.example/careers/jobs/swe-123"
    monkeypatch.setattr(
        link_resolver.http, "get",
        lambda url, **kwargs: _response(aggregator, f'<a href="{direct}"><span>Apply now</span></a>'),
    )
    monkeypatch.setattr(link_resolver, "_public_jobright_detail", lambda url: None)
    result = link_resolver.resolve_link(aggregator, now=100)
    assert result["status"] == "resolved"
    assert result["resolved_url"] == direct


def test_resolver_uses_public_detail_apply_link_as_fallback(monkeypatch):
    aggregator = "https://jobright.ai/jobs/info/abc123"
    direct = "https://boards.greenhouse.io/acme/jobs/123"
    monkeypatch.setattr(
        link_resolver.http, "get",
        lambda url, **kwargs: _response(aggregator, "<html>no application href</html>"),
    )
    monkeypatch.setattr(
        link_resolver, "_public_jobright_detail",
        lambda url: {"jobResult": {"originalUrl": direct}},
    )
    result = link_resolver.resolve_link(aggregator, now=100)
    assert result["status"] == "resolved"
    assert result["resolved_url"] == direct


def test_resolver_marks_jobright_deleted_metadata_as_expired(monkeypatch):
    aggregator = "https://jobright.ai/jobs/info/abc123"
    monkeypatch.setattr(
        link_resolver.http, "get",
        lambda url, **kwargs: _response(aggregator, "<html>visitor variant</html>"),
    )
    monkeypatch.setattr(
        link_resolver, "_public_jobright_detail",
        lambda url: {"result": {"jobDetail": {"jobResult": {"isDeleted": True}}}},
    )

    result = link_resolver.resolve_link(aggregator, now=100)

    assert result["status"] == "closed"
    assert result["posting_status"] == "expired"
    assert result["page_signal_version"] == link_resolver.JOBRIGHT_PAGE_SIGNAL_VERSION
    assert "deleted" in result["reason"].lower()


def test_resolver_keeps_aggregator_when_no_direct_link(monkeypatch):
    aggregator = "https://jobright.ai/jobs/info/abc123"
    monkeypatch.setattr(
        link_resolver.http, "get",
        lambda url, **kwargs: _response(aggregator, "<html>company overview only</html>"),
    )
    monkeypatch.setattr(link_resolver, "_public_jobright_detail", lambda url: None)
    job = Job(company="Acme", title="SWE", url=aggregator, source="jobright")
    result = link_resolver.resolve_job(job, now=100)
    assert result["status"] == "checked_no_direct"
    assert job.url == aggregator
    assert job.alternate_urls == []


def test_cached_resolution_avoids_rechecking(monkeypatch):
    aggregator = "https://jobright.ai/jobs/info/abc123"
    direct = "https://jobs.ashbyhq.com/acme/role-123"
    job = Job(company="Acme", title="SWE", url=aggregator, source="jobright")
    existing = {
        "url": aggregator,
        "link_resolution": {
            "status": "resolved", "checked_at": 90,
            "original_url": aggregator, "resolved_url": direct, "ats": "ashby",
        },
    }
    monkeypatch.setattr(link_resolver, "resolve_link", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError()))
    result = link_resolver.resolve_job(job, existing=existing, now=100)
    assert result["status"] == "resolved"
    assert job.url == direct
    assert job.alternate_urls == [aggregator]
    assert job.ats == "ashby"


def test_old_jobright_no_direct_cache_rechecks_for_page_signal(monkeypatch):
    aggregator = "https://jobright.ai/jobs/info/abc123"
    job = Job(company="Acme", title="SWE", url=aggregator, source="jobright")
    existing = {
        "url": aggregator,
        "link_resolution": {
            "status": "checked_no_direct", "checked_at": 90,
            "original_url": aggregator,
        },
    }
    monkeypatch.setattr(
        link_resolver, "resolve_link",
        lambda *args, **kwargs: {
            "status": "closed", "posting_status": "expired",
            "checked_at": 100, "original_url": aggregator,
            "page_signal_version": link_resolver.JOBRIGHT_PAGE_SIGNAL_VERSION,
        },
    )

    result = link_resolver.resolve_job(job, existing=existing, now=100)

    assert result["status"] == "closed"


def test_closed_aggregator_signal_does_not_close_direct_variant():
    direct = Job(
        company="Acme", title="SWE", url="https://jobs.lever.co/acme/role-123",
        source="lever", ats="lever",
    )
    closed_aggregator = Job(
        company="Acme", title="SWE", url="https://jobright.ai/jobs/info/abc123",
        source="jobright", link_resolution={
            "status": "closed", "posting_status": "expired",
        },
    )

    _merge_job_sighting(direct, closed_aggregator)

    assert direct.link_resolution == {}
    assert closed_aggregator.url in direct.alternate_urls


def test_cross_source_merge_requires_compatible_identity():
    aggregator_url = "https://jobright.ai/jobs/info/abc123"
    direct_url = "https://company.wd5.myworkdayjobs.com/site/job/Acme/SWE_1"
    jobs = {
        "jobright-id": {
            "id": "jobright-id", "company": "Acme", "title": "Software Engineer",
            "url": aggregator_url, "source": "jobright", "locations": ["New York, NY"],
            "link_resolution": {"status": "resolved", "resolved_url": direct_url},
        },
        "direct-id": {
            "id": "direct-id", "company": "Acme", "title": "Software Engineer",
            "url": direct_url, "source": "workday", "ats": "workday",
            "locations": ["New York, NY"],
        },
    }
    collapsed, aliases, merged = collapse_cross_source_jobs(jobs)
    assert merged == 1
    assert aliases == {"jobright-id": "direct-id"}
    assert collapsed["direct-id"]["alternate_urls"] == [aggregator_url]
    assert "jobright" in collapsed["direct-id"]["source_variants"]


def test_cross_source_ambiguous_same_title_is_preserved():
    jobs = {
        "agg": {
            "id": "agg", "company": "Acme", "title": "Software Engineer",
            "url": "https://jobright.ai/jobs/info/abc", "source": "jobright",
            "locations": ["United States"],
        },
        "ny": {
            "id": "ny", "company": "Acme", "title": "Software Engineer",
            "url": "https://jobs.lever.co/acme/ny", "source": "lever", "ats": "lever",
            "locations": ["New York, NY"],
        },
        "sf": {
            "id": "sf", "company": "Acme", "title": "Software Engineer",
            "url": "https://jobs.lever.co/acme/sf", "source": "lever", "ats": "lever",
            "locations": ["San Francisco, CA"],
        },
    }
    collapsed, aliases, merged = collapse_cross_source_jobs(jobs)
    assert merged == 0
    assert aliases == {}
    assert set(collapsed) == {"agg", "ny", "sf"}


def test_feed_dedupe_keeps_all_provenance_links():
    aggregator = Job(
        company="Acme", title="SWE", url="https://jobright.ai/jobs/info/abc",
        source="jobright", source_url="https://github.com/jobright-ai/board",
        link_resolution={"status": "resolved", "resolved_url": "https://jobs.lever.co/acme/123"},
    )
    direct = Job(
        company="Acme", title="SWE", url="https://jobs.lever.co/acme/123",
        source="lever", source_url="https://jobs.lever.co/acme",
        ats="lever",
    )
    selected, dropped = _unique_discovered_jobs([aggregator, direct])
    assert dropped == 1
    # The stable radar identity keeps one role record while retaining the
    # aggregator URL as provenance instead of silently losing that sighting.
    assert len(selected) == 1
    assert selected[0].url == direct.url
    assert aggregator.url in selected[0].alternate_urls


def test_source_only_lead_does_not_contaminate_fresh_verified_ats_sighting():
    direct = "https://boards.greenhouse.io/acme/jobs/123"
    official = Job(
        company="Acme", title="Software Engineer", url=direct,
        source="greenhouse", ats="greenhouse",
        description="This entry-level role welcomes recent graduates.",
    )
    lead = Job(
        company="Acme", title="Software Engineer", url=direct,
        source="zero2sudo_notion", source_url="https://notion.example/lead",
        source_board="public:zero2sudo_notion:source_only",
        link_resolution={
            "status": "source_only", "reason": "curated social lead",
            "source_note": "Apply by Nov 30, 2026.",
        },
    )

    selected, dropped = _unique_discovered_jobs([lead, official])

    assert dropped == 1 and len(selected) == 1
    resolution = selected[0].link_resolution
    assert resolution.get("status") != "source_only"
    assert resolution["source_only_provenance"][0]["source_note"] == (
        "Apply by Nov 30, 2026."
    )
    _, alert_ok, _ = gates(selected[0])
    assert alert_ok is True


def test_source_only_existing_sighting_preserves_direct_state_and_note():
    direct = "https://boards.greenhouse.io/acme/jobs/123"
    target = {
        "id": "acme", "company": "Acme", "title": "Software Engineer",
        "url": direct, "source": "greenhouse", "ats": "greenhouse",
        "link_resolution": {"status": "resolved", "resolved_url": direct},
    }
    lead = Job(
        company="Acme", title="Software Engineer", url=direct,
        source="zero2sudo_notion", source_url="https://notion.example/lead",
        source_board="public:zero2sudo_notion:source_only",
        link_resolution={"status": "source_only", "source_note": "Curator note"},
    )

    _merge_record_sighting(target, lead.to_record())

    assert target["link_resolution"]["status"] == "resolved"
    assert target["link_resolution"]["source_only_provenance"][0]["source_note"] == (
        "Curator note"
    )


def test_source_only_existing_unverified_target_keeps_top_level_status():
    direct = "https://boards.greenhouse.io/acme/jobs/124"
    target = {
        "id": "acme-unverified", "company": "Acme", "title": "Software Engineer",
        "url": direct, "source": "greenhouse", "ats": "greenhouse",
    }
    lead = Job(
        company="Acme", title="Software Engineer", url=direct,
        source="zero2sudo_notion", source_url="https://notion.example/lead",
        source_board="public:zero2sudo_notion:source_only",
        link_resolution={"status": "source_only", "source_note": "Curator note"},
    )

    _merge_record_sighting(target, lead.to_record())

    assert target["link_resolution"]["status"] == "source_only"
    assert target["link_resolution"]["source_note"] == "Curator note"


def test_source_only_lead_does_not_mask_existing_posting_facts():
    direct = "https://boards.greenhouse.io/acme/jobs/127"
    target = {
        "id": "acme-scraped", "company": "Acme", "title": "Software Engineer",
        "url": direct, "source": "greenhouse", "ats": "greenhouse",
        "posting": {"ranking_evidence": {"technical_depth": "build software"}},
    }
    lead = Job(
        company="Acme", title="Software Engineer", url=direct,
        source="zero2sudo_notion", source_url="https://notion.example/lead",
        link_resolution={"status": "source_only", "source_note": "Curator note"},
    )

    _merge_record_sighting(target, lead.to_record())

    assert target["link_resolution"].get("status") != "source_only"
    assert target["link_resolution"]["source_only_provenance"]


def test_fresh_direct_evidence_replaces_stale_eligibility_facts():
    direct = "https://boards.greenhouse.io/acme/jobs/125"
    target = {
        "id": "acme-stale", "company": "Acme", "title": "Software Engineer",
        "url": direct, "source": "greenhouse", "ats": "greenhouse",
        "eligibility_evidence": {
            "status": "review", "source": "missing-description",
            "required_floor": None,
        },
        "explicit_new_grad": False,
    }
    official = Job(
        company="Acme", title="Software Engineer", url=direct,
        source="greenhouse", ats="greenhouse",
        description=(
            "New Grad Software Engineer. Required qualifications: 3+ years "
            "of software engineering experience."
        ),
    )
    _, alert_ok, _ = gates(official)
    assert alert_ok is False
    sighting = official.to_record()
    sighting["_fresh_employer_evidence"] = True

    changed = _merge_record_sighting(target, sighting)

    assert changed is True
    assert target["eligibility_evidence"]["required_floor"] == 3
    assert target["eligibility_evidence"]["experience_note"]
    assert target["explicit_new_grad"] is True


def test_fresh_direct_evidence_clears_old_source_only_gate_but_keeps_provenance():
    direct = "https://boards.greenhouse.io/acme/jobs/128"
    target = {
        "id": "acme-source-only", "company": "Acme", "title": "Software Engineer",
        "url": direct, "source": "greenhouse", "ats": "greenhouse",
        "link_resolution": {
            "status": "source_only", "source_note": "Curator note",
        },
    }
    official = Job(
        company="Acme", title="Software Engineer", url=direct,
        source="greenhouse", ats="greenhouse",
        description="This entry-level role welcomes recent graduates.",
    )
    gates(official)
    sighting = official.to_record()
    sighting["_fresh_employer_evidence"] = True

    _merge_record_sighting(target, sighting)

    assert target["link_resolution"].get("status") != "source_only"
    assert target["link_resolution"]["source_only_provenance"][0]["source_note"] == (
        "Curator note"
    )
    assert target["eligibility_evidence"]["source"] == "description"


def test_source_only_fresh_sighting_cannot_replace_verified_evidence():
    direct = "https://boards.greenhouse.io/acme/jobs/126"
    target = {
        "id": "acme-verified", "company": "Acme", "title": "Software Engineer",
        "url": direct, "source": "greenhouse", "ats": "greenhouse",
        "link_resolution": {"status": "resolved", "resolved_url": direct},
        "eligibility_evidence": {"status": "verified", "source": "description"},
        "explicit_new_grad": True,
    }
    lead = Job(
        company="Acme", title="Software Engineer", url=direct,
        source="zero2sudo_notion", source_url="https://notion.example/lead",
        link_resolution={"status": "source_only", "source_note": "Curator note"},
        eligibility_evidence={"status": "review", "source": "source-only"},
    )
    sighting = lead.to_record()
    sighting["_fresh_employer_evidence"] = True

    _merge_record_sighting(target, sighting)

    assert target["eligibility_evidence"]["status"] == "verified"
    assert target["explicit_new_grad"] is True
    assert target["link_resolution"]["status"] == "resolved"


def test_curator_sighting_does_not_demote_persisted_employer_evidence():
    bare_sighting = Job(
        company="Acme", title="Software Engineer Intern",
        url="https://jobs.lever.co/acme/role-123", source="lever", ats="lever",
        internship_eligibility={"internship_signal": "title"},
    )
    assert not radar_main._has_independent_employer_evidence(bare_sighting)
    for evidence in (
        {"eligibility_evidence": {"status": "verified", "source": "description"}},
        {"internship_eligibility": {"internship_signal": "body", "status": "explicit"}},
    ):
        target = {
            "id": "verified", "company": "Acme", "title": "Software Engineer",
            "url": "https://jobs.lever.co/acme/role-123", "source": "lever",
            "ats": "lever", **evidence,
        }
        lead = Job(
            company="Acme", title="Software Engineer", url=target["url"],
            source="zero2sudo_notion",
            link_resolution={"status": "source_only", "source_note": "curator note"},
        )
        _merge_record_sighting(target, lead.to_record())
        assert target["link_resolution"].get("status") != "source_only"
        assert target["link_resolution"]["source_only_provenance"][0]["source_note"] == "curator note"


def test_outputs_show_direct_and_fallback_links():
    job = {
        "id": "a" * 16, "company": "Acme", "title": "SWE", "url": "https://jobs.lever.co/acme/123",
        "alternate_urls": ["https://jobright.ai/jobs/info/abc"],
        "source": "jobright", "source_url": "https://github.com/jobright-ai/board",
        "locations": ["New York, NY"], "score": 88, "sector": "tech",
        "alert_ok": True, "first_seen": int(time.time()), "posted_at": int(time.time()),
    }
    line = format_line(job)
    dashboard = render_dashboard({job["id"]: job}, {}, [])
    assert "https://jobs.lever.co/acme/123" in line
    assert "Jobright fallback" in line
    assert "https://jobright.ai/jobs/info/abc" in dashboard


def test_resolve_links_cmd_batches_open_records_in_parallel(monkeypatch):
    jobs = {
        "open-a": {
            "id": "open-a", "company": "Acme", "title": "SWE",
            "url": "https://jobright.ai/jobs/info/a", "source": "jobright",
            "posting_status": "open", "alert_ok": True, "score": 90,
        },
        "open-b": {
            "id": "open-b", "company": "Beta", "title": "SWE",
            "url": "https://jobright.ai/jobs/info/b", "source": "jobright",
            "posting_status": "open", "score": 80,
        },
        "already-expired": {
            "id": "already-expired", "company": "Gamma", "title": "SWE",
            "url": "https://jobright.ai/jobs/info/c", "source": "jobright",
            "posting_status": "expired", "score": 99,
        },
    }
    calls = []

    def fake_resolve(job, existing=None, now=None):
        calls.append(job.url)
        return {"status": "checked_no_direct", "checked_at": now,
                "original_url": job.url, "page_signal_version": 1}

    monkeypatch.setenv("RADAR_LINK_RESOLVE_LIMIT", "2")
    monkeypatch.setenv("RADAR_LINK_RESOLVE_WORKERS", "2")
    monkeypatch.setattr(radar_main.state, "jobs", lambda: jobs)
    monkeypatch.setattr(radar_main.state, "applied", lambda: [])
    monkeypatch.setattr(radar_main.state, "companies", lambda: {})
    monkeypatch.setattr(radar_main.state, "load", lambda name, default=None: default)
    monkeypatch.setattr(radar_main.state, "save", lambda name, value: None)
    monkeypatch.setattr(radar_main, "write_outputs", lambda *args: None)
    monkeypatch.setattr(radar_main, "_repair_duplicate_job_state",
                        lambda current, applied: (current, False))
    monkeypatch.setattr(link_resolver, "resolve_job", fake_resolve)

    assert radar_main.resolve_links_cmd() == 0
    assert sorted(calls) == [
        "https://jobright.ai/jobs/info/a",
        "https://jobright.ai/jobs/info/b",
    ]
    assert jobs["open-a"]["link_resolution"]["status"] == "checked_no_direct"
    assert jobs["open-b"]["link_resolution"]["status"] == "checked_no_direct"
    assert "link_resolution" not in jobs["already-expired"]
