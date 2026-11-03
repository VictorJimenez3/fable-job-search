from radar import main
from radar.models import Job
from radar.score import gates, score
from radar.sources.public_feeds import PUBLIC_FETCHERS


def test_public_discovery_sources_are_registered_and_fail_independently(monkeypatch):
    assert set(PUBLIC_FETCHERS).issubset(main.AGG_SOURCES)
    from radar.sources.public_feeds import PUBLIC_INTERNSHIP_FETCHERS
    assert set(PUBLIC_INTERNSHIP_FETCHERS).issubset(main.AGG_SOURCES_INTERNSHIP)

    def failed_source():
        raise ValueError("unexpected public feed schema")

    monkeypatch.setattr(main, "AGG_SOURCES", {"broken": failed_source, "empty": lambda: []})
    monkeypatch.setattr(main, "profile_id", lambda: "new_grad")
    jobs, stats, healthy = main._fetch_aggregators(set())
    assert jobs == []
    assert "error:" in stats["broken"]
    assert healthy == {"aggregator:empty"}


def test_aggregator_normalization_preserves_adapter_attribution(monkeypatch):
    source = Job(company="Acme", title="Engineering Intern", url="https://jobs.example/1",
                 source="headcount_engineering_internships",
                 source_board="public:headcount_engineering_internships; Headcount Labs (ODbL 1.0)")
    monkeypatch.setattr(main, "AGG_SOURCES", {"headcount": lambda: [source]})
    monkeypatch.setattr(main, "profile_id", lambda: "new_grad")
    jobs, _, _ = main._fetch_aggregators(set())
    assert jobs[0].source_board == "aggregator:headcount"
    assert any("Headcount Labs" in value for value in jobs[0].source_board_variants)


def test_rebuild_keeps_description_eligibility_and_source_only_uncertainty(monkeypatch):
    from radar import culture, sponsorship

    monkeypatch.setattr(culture, "write_outputs", lambda: None)
    monkeypatch.setattr(sponsorship, "load", lambda: {})
    monkeypatch.setattr(sponsorship, "annotate_record", lambda *args: None)
    verified = Job(company="Acme", title="Software Engineer", source="greenhouse",
                   url="https://boards.greenhouse.io/acme/jobs/123", locations=["New York, US"],
                   description="This entry-level role welcomes recent graduates.")
    gates(verified)
    score(verified, {}, 1_800_000_000)
    source_only = Job(company="Acme", title="Software Engineer", source="simplify",
                      url="https://boards.greenhouse.io/acme/jobs/124", locations=["New York, US"])
    gates(source_only)
    score(source_only, {}, 1_800_000_000)
    # The legacy ID intentionally does not include URL, so name each row explicitly.
    rows = {"verified": {**verified.to_record(), "id": "verified"},
            "uncertain": {**source_only.to_record(), "id": "uncertain"}}
    main._rebuild_scores(rows, {}, 1_800_000_000)
    assert rows["verified"]["alert_ok"] is True
    assert rows["verified"]["explicit_new_grad"] is True
    assert rows["verified"]["eligibility_evidence"]["source"] == "description"
    assert rows["uncertain"]["alert_ok"] is False
    assert rows["uncertain"]["explicit_new_grad"] is False
