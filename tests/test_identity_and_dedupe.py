from radar.dedupe import collapse_cross_source_jobs, collapse_jobs, collapse_location_variants
from radar.identity import canonical_url
from radar.main import _unique_discovered_jobs
from radar.models import Job


def test_canonical_url_removes_tracking_but_keeps_job_parameters():
    assert canonical_url(
        "HTTPS://Example.com/role/?utm_source=email&ref=board&gh_jid=123#apply"
    ) == "https://example.com/role?gh_jid=123"


def test_collapse_jobs_prefers_direct_ats_and_returns_alias():
    jobs = {
        "aggregator-id": {
            "id": "aggregator-id", "company": "Acme", "title": "SWE",
            "url": "https://jobs.acme.test/role/?utm_source=feed",
            "source": "simplify", "score": 70, "first_seen": 1,
        },
        "ats-id": {
            "id": "ats-id", "company": "Acme", "title": "Software Engineer",
            "url": "https://jobs.acme.test/role", "source": "greenhouse",
            "ats": "greenhouse", "score": 80, "first_seen": 2,
        },
    }
    collapsed, aliases, merged = collapse_jobs(jobs)
    assert merged == 1
    assert set(collapsed) == {"ats-id"}
    assert aliases == {"aggregator-id": "ats-id"}
    assert "simplify" in collapsed["ats-id"]["source_variants"]


def test_discovered_feed_dedupe_keeps_one_best_provenance():
    aggregator = Job(company="Acme", title="SWE", url="https://acme.test/role",
                     source="simplify")
    ats = Job(company="Acme", title="Software Engineer", url="https://acme.test/role/",
              source="greenhouse", ats="greenhouse", description="full posting")
    selected, dropped = _unique_discovered_jobs([aggregator, ats])
    assert dropped == 1
    assert len(selected) == 1
    assert selected[0].source == "greenhouse"


def test_discovered_family_dedupe_preserves_audit_metadata_on_fresh_job():
    official = Job(
        company="Google", title="Software Engineer - Campus",
        url="https://www.google.com/about/careers/applications/jobs/results/123",
        source="simplify", locations=["Mountain View, CA"],
    )
    variant = Job(
        company="Google", title="Software Engineer, Early Career, Campus",
        url="https://jobright.ai/jobs/info/mountain-view", source="jobright",
        source_url="https://github.com/jobright-ai/board",
        locations=["Mountain View, CA"],
    )
    selected, dropped = _unique_discovered_jobs([official, variant])
    assert dropped == 1
    assert len(selected) == 1
    assert selected[0].posting_family_id == selected[0].id
    assert selected[0].posting_identity["variant_count"] == 2


def test_location_family_merges_google_style_official_and_jobright_rows():
    jobs = {
        "google-official": {
            "id": "google-official", "company": "Google",
            "title": "Software Engineer - Campus",
            "url": "https://www.google.com/about/careers/applications/jobs/results/123",
            "source": "simplify", "locations": ["Mountain View, CA", "Cambridge, MA"],
            "last_seen_at": 20,
        },
        "jobright-mv": {
            "id": "jobright-mv", "company": "Google",
            "title": "Software Engineer, Early Career, Campus",
            "url": "https://jobright.ai/jobs/info/mountain-view",
            "source": "jobright", "source_url": "https://github.com/jobright-ai/board",
            "locations": ["Mountain View, CA, United States"], "last_seen_at": 10,
        },
        "jobright-cambridge": {
            "id": "jobright-cambridge", "company": "Google",
            "title": "Software Engineer, Early Career, Campus",
            "url": "https://jobright.ai/jobs/info/cambridge",
            "source": "jobright", "source_url": "https://github.com/jobright-ai/board",
            "locations": ["Cambridge, MA, United States"], "last_seen_at": 11,
        },
    }
    collapsed, aliases, merged = collapse_cross_source_jobs(jobs)
    assert merged == 2
    assert set(collapsed) == {"google-official"}
    assert aliases == {"jobright-mv": "google-official", "jobright-cambridge": "google-official"}
    assert collapsed["google-official"]["posting_family_id"] == "google-official"
    assert collapsed["google-official"]["posting_identity"]["variant_count"] == 3
    assert set(collapsed["google-official"]["locations"]) == {
        "Mountain View, CA", "Cambridge, MA", "Mountain View, CA, United States",
        "Cambridge, MA, United States",
    }


def test_same_board_marked_location_variants_collapse_without_an_official_row():
    jobs = {
        "mv": {
            "id": "mv", "company": "Acme", "title": "Software Engineer, New Grad",
            "url": "https://jobright.ai/jobs/info/mv", "source": "jobright",
            "source_url": "https://github.com/jobright-ai/board",
            "locations": ["Mountain View, CA"], "posted_at": 172800,
        },
        "ny": {
            "id": "ny", "company": "Acme", "title": "Software Engineer, New Grad",
            "url": "https://jobright.ai/jobs/info/ny", "source": "jobright",
            "source_url": "https://github.com/jobright-ai/board", "locations": ["New York, NY"], "posted_at": 172800,
        },
    }
    collapsed, aliases, merged = collapse_location_variants(jobs)
    assert merged == 1
    assert len(collapsed) == 1
    winner = next(iter(collapsed.values()))
    assert aliases == {"ny": winner["id"]} or aliases == {"mv": winner["id"]}
    assert winner["posting_identity"]["variant_count"] == 2


def test_same_board_marked_rows_on_different_days_remain_separate_requisitions():
    jobs = {
        "day-one": {
            "id": "day-one", "company": "Acme", "title": "Software Engineer, New Grad",
            "url": "https://jobright.ai/jobs/info/day-one", "source": "jobright",
            "source_url": "https://github.com/jobright-ai/board",
            "locations": ["Mountain View, CA"], "posted_at": 172800,
        },
        "day-two": {
            "id": "day-two", "company": "Acme", "title": "Software Engineer, New Grad",
            "url": "https://jobright.ai/jobs/info/day-two", "source": "jobright",
            "source_url": "https://github.com/jobright-ai/board",
            "locations": ["New York, NY"], "posted_at": 172800 + 86400,
        },
    }
    collapsed, aliases, merged = collapse_location_variants(jobs)
    assert merged == 0
    assert aliases == {}
    assert set(collapsed) == {"day-one", "day-two"}


def test_same_title_direct_requisitions_remain_separate_when_ambiguous():
    jobs = {
        "one": {
            "id": "one", "company": "Acme", "title": "Software Engineer",
            "url": "https://jobs.acme.test/one", "source": "greenhouse", "ats": "greenhouse",
            "locations": ["New York, NY"],
        },
        "two": {
            "id": "two", "company": "Acme", "title": "Software Engineer",
            "url": "https://jobs.acme.test/two", "source": "greenhouse", "ats": "greenhouse",
            "locations": ["New York, NY"],
        },
        "feed": {
            "id": "feed", "company": "Acme", "title": "Software Engineer",
            "url": "https://jobright.ai/jobs/info/feed", "source": "jobright",
            "locations": ["New York, NY"],
        },
    }
    collapsed, aliases, merged = collapse_cross_source_jobs(jobs)
    assert merged == 0
    assert aliases == {}
    assert set(collapsed) == {"one", "two", "feed"}
