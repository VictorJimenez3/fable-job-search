from copy import deepcopy

import pytest

import radar.main as radar_main
from radar.dedupe import collapse_jobs, find_existing_official_match
from radar.main import _merge_record_sighting, _promote_existing_official_record
from radar.models import Job


def _aggregator_record(*, job_id="legacy", title="Software Engineer, New Grad",
                       url="https://jobright.ai/jobs/info/acme-123"):
    return {
        "id": job_id,
        "company": "Acme",
        "title": title,
        "url": url,
        "source": "jobright",
        "locations": ["New York, NY"],
        "posted_at": 172800,
        "manual_added": True,
        "first_seen": 10,
        "stage": "interview",
    }


def _official_sighting(*, url="https://boards.greenhouse.io/acme/jobs/123",
                       title="Software Engineer, New Grad"):
    return Job(
        company="Acme", title=title, url=url, source="greenhouse",
        ats="greenhouse", locations=["New York, NY"], posted_at=172800,
    ).to_record()


def test_existing_aggregator_is_promoted_in_place_and_owner_state_survives():
    legacy = _aggregator_record()
    snapshot = {legacy["id"]: legacy}
    incoming = _official_sighting()

    match = find_existing_official_match(incoming, snapshot, [incoming])
    assert match == legacy["id"]
    assert _promote_existing_official_record(legacy, incoming) is True
    _merge_record_sighting(legacy, incoming)

    assert legacy["id"] == "legacy"
    assert legacy["url"] == incoming["url"]
    assert "https://jobright.ai/jobs/info/acme-123" in legacy["alternate_urls"]
    assert legacy["source"] == "greenhouse"
    assert "jobright" in legacy["source_variants"]
    assert legacy["manual_added"] is True
    assert legacy["first_seen"] == 10
    assert legacy["stage"] == "interview"
    assert legacy["link_resolution"]["status"] == "resolved"
    assert "official employer URL upgrade" in legacy["posting_identity"]["matched_by"]


def test_empty_state_same_batch_uses_one_stable_target_for_both_arrival_orders():
    aggregator = _aggregator_record(job_id="legacy")
    official = _official_sighting()
    aggregator["_radar_id"] = "legacy"
    official["_radar_id"] = "official"

    from radar.dedupe import build_official_match_index

    batch = [aggregator, official]
    index = build_official_match_index({}, batch)
    assert find_existing_official_match(
        official, {}, batch, index=index
    ) == "legacy"
    assert find_existing_official_match(
        aggregator, {}, batch, index=index
    ) == "official"


def test_empty_state_same_batch_keeps_two_official_requisitions_ambiguous():
    aggregator = _aggregator_record(job_id="legacy")
    first = _official_sighting(url="https://boards.greenhouse.io/acme/jobs/123")
    second = _official_sighting(url="https://boards.greenhouse.io/acme/jobs/124")
    aggregator["_radar_id"] = "legacy"
    first["_radar_id"] = "first"
    second["_radar_id"] = "second"

    from radar.dedupe import build_official_match_index

    batch = [aggregator, first, second]
    index = build_official_match_index({}, batch)
    assert find_existing_official_match(first, {}, batch, index=index) is None


@pytest.mark.parametrize("official_first", [False, True])
def test_main_crawl_same_batch_persists_one_alert_for_both_arrival_orders(monkeypatch, official_first):
    aggregator = Job(
        company="Acme", title="Software Engineer, New Grad",
        url="https://jobright.ai/jobs/info/acme-123", source="jobright",
        locations=["New York, NY"],
    )
    official = Job(
        company="Acme", title="Software Engineer, New Grad",
        url="https://boards.greenhouse.io/acme/jobs/123", source="greenhouse",
        ats="greenhouse", locations=["New York, NY"],
        description="Acme is hiring software engineers. New graduates welcome.",
    )
    jobs_state: dict = {}
    applied: list = []
    registry: dict = {}
    alert_history: list = []
    runs: list = []

    def fake_env(key, default=""):
        return {
            "RADAR_DEFER_DELIVERY": "true",
            "RADAR_DISABLE_LINK_RESOLUTION": "true",
        }.get(key, default)

    def fake_load(name, default=None):
        return {
            "alert_history.json": alert_history,
            "runs.json": runs,
            "web_state.json": {},
            "job_aliases.json": {},
        }.get(name, deepcopy(default))

    def fake_save(name, value):
        if name == "jobs.json":
            jobs_state.clear()
            jobs_state.update(deepcopy(value))
        elif name == "alert_history.json":
            alert_history[:] = deepcopy(value)
        elif name == "runs.json":
            runs[:] = deepcopy(value)

    if official_first:
        aggregator_feed, official_feed = [official], [aggregator]
    else:
        aggregator_feed, official_feed = [aggregator], [official]

    monkeypatch.setattr(radar_main, "profile_id", lambda: "new_grad")
    monkeypatch.setattr(radar_main, "profile", lambda: {
        "thresholds": {"alert": 60, "max_posting_age_days": 30},
        "llm": {"rerank_band": 0},
    })
    monkeypatch.setattr(radar_main, "env", fake_env)
    monkeypatch.setattr(radar_main.state, "companies", lambda: registry)
    monkeypatch.setattr(radar_main.state, "jobs", lambda: jobs_state)
    monkeypatch.setattr(radar_main.state, "applied", lambda: applied)
    monkeypatch.setattr(radar_main.state, "feedback", lambda: {})
    monkeypatch.setattr(radar_main.state, "load", fake_load)
    monkeypatch.setattr(radar_main.state, "save", fake_save)
    monkeypatch.setattr(radar_main.discovery, "seed_registry", lambda *args: 0)
    monkeypatch.setattr(radar_main.discovery, "harvest", lambda *args, **kwargs: 0)
    monkeypatch.setattr(radar_main.discovery, "probe_new", lambda *args, **kwargs: (0, 0))
    monkeypatch.setattr(radar_main, "_repair_duplicate_job_state", lambda jobs, rows: (jobs, False))
    monkeypatch.setattr(radar_main, "regate", lambda jobs: 0)
    monkeypatch.setattr(radar_main, "build_preference_profile", lambda *args: {"sample_count": 0})
    monkeypatch.setattr(radar_main, "load_score_preferences", lambda: {})
    monkeypatch.setattr(radar_main, "_fetch_aggregators", lambda disabled: (
        [deepcopy(job) for job in aggregator_feed], {"ok": 1}, {"aggregator:test"}))
    monkeypatch.setattr(radar_main, "_fetch_ats", lambda registry, disabled: (
        [deepcopy(job) for job in official_feed], {"ok": 1}, {"ats:test"}))
    monkeypatch.setattr(radar_main, "_resolve_discovered_links", lambda jobs, *args: (
        jobs, {"attempted": 0, "resolved": 0, "closed": 0, "unchanged": 0, "errors": 0}))
    monkeypatch.setattr(radar_main, "gates", lambda job: (True, True, []))

    def fake_score(job, *args, **kwargs):
        job.score = 80
        job.alert_ok = True

    monkeypatch.setattr(radar_main, "score", fake_score)
    monkeypatch.setattr(radar_main, "_rebuild_scores", lambda jobs, *args: (len(jobs), len(jobs)))
    monkeypatch.setattr(radar_main.lifecycle, "reconcile", lambda *args, **kwargs: {
        "expired": 0, "reopened": 0,
    })
    monkeypatch.setattr(radar_main, "write_outputs", lambda *args: None)
    monkeypatch.setattr(radar_main, "deliver_alerts", lambda: None)
    monkeypatch.setattr("radar.culture.write_outputs", lambda: None)
    monkeypatch.setattr("radar.company_research.prepare_for_jobs", lambda *args, **kwargs: 0)
    monkeypatch.setattr("radar.llm.available", lambda *args: False)
    monkeypatch.setattr("radar.posting.scrape_pass", lambda *args, **kwargs: {})
    monkeypatch.setattr("radar.notion_sync.archive_terminal_pages", lambda *args: 0)

    assert radar_main.crawl() == 0
    assert len(jobs_state) == 1
    assert len(alert_history) == 1
    survivor = next(iter(jobs_state.values()))
    assert survivor["url"] == official.url
    assert aggregator.url in survivor["alternate_urls"]
    assert survivor["source"] == "greenhouse"


def test_reverse_arrival_attaches_aggregator_without_downgrading_official():
    official = _official_sighting()
    official["id"] = "official"
    snapshot = {"official": official}
    aggregator = _aggregator_record()

    match = find_existing_official_match(aggregator, snapshot, [aggregator])
    assert match == "official"
    assert _promote_existing_official_record(official, aggregator) is False
    _merge_record_sighting(official, aggregator)

    assert official["url"] == "https://boards.greenhouse.io/acme/jobs/123"
    assert aggregator["url"] in official["alternate_urls"]


def test_source_only_direct_url_does_not_promote_aggregator_primary():
    legacy = _aggregator_record()
    direct = "https://boards.greenhouse.io/acme/jobs/123"
    legacy["alternate_urls"] = [direct]
    source_only = _official_sighting(url=direct)
    source_only["link_resolution"] = {
        "status": "source_only", "source_note": "curator URL"
    }

    assert find_existing_official_match(
        source_only, {"legacy": legacy}, [source_only]
    ) == "legacy"
    assert _promote_existing_official_record(legacy, source_only) is False
    _merge_record_sighting(legacy, source_only)

    assert legacy["url"] == "https://jobright.ai/jobs/info/acme-123"
    assert legacy["link_resolution"]["status"] == "source_only"


def test_fallback_refuses_multiple_incoming_requisitions():
    legacy = _aggregator_record()
    first = _official_sighting(url="https://boards.greenhouse.io/acme/jobs/123")
    second = _official_sighting(url="https://boards.greenhouse.io/acme/jobs/124")

    assert find_existing_official_match(first, {"legacy": legacy}, [first, second]) is None


def test_fallback_does_not_use_ats_key_as_shared_requisition_proof():
    legacy = _aggregator_record(title="Software Engineer")
    incoming = _official_sighting(
        url="https://boards.greenhouse.io/acme/jobs/999",
        title="Software Engineer",
    )

    assert find_existing_official_match(incoming, {"legacy": legacy}, [incoming]) is None


def test_fallback_refuses_aggregator_ambiguity_split_between_state_and_batch():
    legacy = _aggregator_record()
    second = _aggregator_record(
        job_id="second", url="https://jobright.ai/jobs/info/acme-456"
    )
    incoming = _official_sighting(url="https://boards.greenhouse.io/acme/jobs/999")

    assert find_existing_official_match(
        incoming, {"legacy": legacy}, [incoming, second]
    ) is None


def test_reverse_fallback_refuses_aggregator_ambiguity_split_between_state_and_batch():
    official = _official_sighting()
    official["id"] = "official"
    incoming = _aggregator_record()
    second = _aggregator_record(
        job_id="second", url="https://jobright.ai/jobs/info/acme-456"
    )

    assert find_existing_official_match(
        incoming, {"official": official}, [incoming, second]
    ) is None


def test_fallback_refuses_multiple_existing_official_requisitions():
    legacy = _aggregator_record()
    first = _official_sighting(url="https://boards.greenhouse.io/acme/jobs/123")
    second = _official_sighting(url="https://boards.greenhouse.io/acme/jobs/124")
    incoming = _official_sighting(url="https://boards.greenhouse.io/acme/jobs/125")

    assert find_existing_official_match(
        incoming,
        {"legacy": legacy, "first": first, "second": second},
        [incoming],
    ) is None


def test_fallback_does_not_equate_remote_canada_and_remote_us():
    legacy = _aggregator_record()
    legacy["locations"] = ["Remote, Canada"]
    incoming = _official_sighting()
    incoming["locations"] = ["Remote, US"]

    assert find_existing_official_match(incoming, {"legacy": legacy}, [incoming]) is None


def test_durable_exact_merge_prefers_verified_employer_and_keeps_curator_note():
    url = "https://boards.greenhouse.io/acme/jobs/123"
    rows = {
        "official": {
            "id": "official", "company": "Acme", "title": "Software Engineer",
            "url": url, "source": "greenhouse",
            "link_resolution": {"status": "resolved", "resolved_url": url},
        },
        "curator": {
            "id": "curator", "company": "Acme", "title": "Software Engineer",
            "url": url, "source": "zero2sudo_notion",
            "source_url": "https://notion.example/lead",
            "link_resolution": {
                "status": "source_only", "source_note": "Curator note",
            },
        },
    }

    merged, aliases, count = collapse_jobs(rows)

    assert count == 1
    assert aliases == {"curator": "official"}
    survivor = merged["official"]
    assert survivor["source"] == "greenhouse"
    assert survivor["link_resolution"]["status"] == "resolved"
    assert survivor["link_resolution"]["source_only_provenance"][0]["source_note"] == (
        "Curator note"
    )
