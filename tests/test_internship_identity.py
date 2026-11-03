from radar import main, state
from radar.internship_radar import crawl
from radar.models import Job


def _posting(*, url, description, source="lever", title="Software Engineering Intern"):
    return Job(
        company="Acme",
        title=title,
        url=url,
        source=source,
        source_board=f"ats:{source}",
        ats=source,
        locations=["New York, NY"],
        description=description,
        link_resolution={"status": "resolved", "resolved_url": url},
    )


def test_internship_crawl_reconciles_variants_and_updates_existing_source_only(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("RADAR_PROFILE", "internship")
    monkeypatch.setenv("RADAR_DEFER_DELIVERY", "1")
    monkeypatch.setattr(state, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr("radar.internship_radar.write_outputs", lambda *args: None)
    monkeypatch.setattr("radar.discovery.seed_registry", lambda *args: None)
    monkeypatch.setattr("radar.discovery.harvest", lambda *args, **kwargs: 0)
    monkeypatch.setattr("radar.discovery.probe_new", lambda *args, **kwargs: (0, 0))
    monkeypatch.setattr("radar.notion_sync.archive_terminal_pages", lambda *args: 0)
    monkeypatch.setattr("radar.posting.scrape_pass", lambda *args, **kwargs: {})

    state.STATE_DIR.mkdir()
    state.save("jobs.json", {
        "curated": {
            "id": "curated",
            "company": "Acme",
            "title": "Software Engineering Intern",
            "url": "https://jobs.lever.co/acme/role-123",
            "source": "zero2sudo_notion",
            "locations": ["New York, NY"],
            "link_resolution": {
                "status": "source_only",
                "source_note": "curator note",
            },
            "internship_eligibility": {"internship_signal": "source_only"},
            "posting_status": "open",
            "score_version": 7,
            "rules_v": 7,
            "score": 10,
            "alert_ok": False,
        }
    })

    official = _posting(
        url="https://jobs.lever.co/acme/role-123/apply",
        title="Senior Software Engineering Intern",
        description="Current students are not eligible for this senior role.",
    )
    distinct = _posting(
        url="https://jobs.lever.co/acme/role-456",
        description="Current undergraduate students graduating in 2028 may apply.",
    )
    distinct2 = _posting(
        url="https://jobs.lever.co/acme/role-789",
        description="Current undergraduate students graduating in 2028 may apply.",
    )
    # Both input orders are covered by the provider-aware lane dedupe; the
    # official variant must win over the source-only lead either way.
    monkeypatch.setattr(
        main,
        "_fetch_aggregators",
            lambda disabled: ([official, distinct, distinct2], {"feed": 3}, {"aggregator:feed"}),
    )
    monkeypatch.setattr(
        main,
        "_fetch_ats",
        lambda registry, disabled: ([], {}, set()),
    )
    monkeypatch.setattr(main, "deliver_alerts", lambda *args, **kwargs: None)

    crawl()

    rows = state.jobs()
    assert len(rows) == 3
    surviving = next(row for row in rows.values() if "role-123" in row["url"])
    assert surviving["link_resolution"]["status"] == "resolved"
    assert surviving["link_resolution"]["source_only_provenance"]
    assert not surviving.get("alert_ok", False)
    assert any("experienced or doctoral" in reason for reason in surviving["score_reasons"])
    assert surviving["score_version"] == 7
    assert surviving["score_dimensions"]
    assert sum("role-123" in row["url"] for row in rows.values()) == 1
    assert sum("role-456" in row["url"] for row in rows.values()) == 1
    assert sum("role-789" in row["url"] for row in rows.values()) == 1

    # The source-only lead was reconciled into the existing posting; it did
    # not create a second alert-history row.
    alert_history = state.load("alert_history.json", [])
    assert sum("role-123" in row.get("url", "") for row in alert_history) == 0
