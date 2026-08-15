from radar.dedupe import collapse_jobs
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
