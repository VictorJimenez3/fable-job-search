import pytest

import radar.internship_radar as internship_radar
from radar import main, state
from radar.internship_radar import crawl
from radar.models import Job

NOW = 1_793_725_200


def _source_only_row():
    return {
        "id": "nvidia-spring",
        "company": "NVIDIA",
        "title": "Software Engineering Intern",
        "url": "https://jobs.example.com/nvidia/careers/software-engineering-intern",
        "source": "zero2sudo_notion",
        "locations": ["Santa Clara, CA"],
        "link_resolution": {"status": "source_only", "source_note": "curated"},
        "internship_eligibility": {
            "evidence_version": 2,
            "term_options": ["2027-01-01"],
            "term_start": "2027-01-01",
            "internship_signal": "source_only",
        },
        "posting_status": "open",
        "first_seen": NOW - 3600,
        "score": 72,
        "alert_ok": False,
    }


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("RADAR_PROFILE", "internship")
    monkeypatch.setenv("RADAR_DEFER_DELIVERY", "1")
    monkeypatch.setenv("RADAR_INTERNSHIP_SCRAPE_WORKERS", "1")
    monkeypatch.setenv("RADAR_INTERNSHIP_MAX_ALERTS", "35")
    monkeypatch.setattr(internship_radar.time, "time", lambda: NOW)
    monkeypatch.setattr(state, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr("radar.internship_radar.write_outputs", lambda *args: None)
    monkeypatch.setattr("radar.discovery.seed_registry", lambda *args: None)
    monkeypatch.setattr("radar.discovery.harvest", lambda *args, **kwargs: 0)
    monkeypatch.setattr("radar.discovery.probe_new", lambda *args, **kwargs: (0, 0))
    monkeypatch.setattr("radar.notion_sync.archive_terminal_pages", lambda *args: 0)
    monkeypatch.setattr(main, "_fetch_aggregators", lambda disabled: ([], {}, set()))
    monkeypatch.setattr(main, "_fetch_ats", lambda registry, disabled: ([], {}, set()))
    monkeypatch.setattr(main, "deliver_alerts", lambda *args, **kwargs: None)
    state.STATE_DIR.mkdir()


def _employer_text():
    return ("Responsibilities: build software as an intern. Qualifications: "
            "current students may apply for this software engineering internship. " * 5)


def _official_job(url="https://jobs.example.com/nvidia/careers/software-engineering-intern"):
    return Job(company="NVIDIA", title="Software Engineering Intern", url=url,
               source="greenhouse", source_board="ats:greenhouse", ats="greenhouse",
               locations=["Santa Clara, CA"], description=_employer_text(),
               link_resolution={"status": "resolved", "resolved_url": url})


def test_existing_employer_confirmation_creates_one_alert_and_second_crawl_is_quiet(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    state.save("jobs.json", {"nvidia-spring": _source_only_row()})
    text = _employer_text()
    monkeypatch.setattr("radar.quality.fetch_posting", lambda url: (True, text))

    crawl()
    history = state.load("alert_history.json", [])
    assert len(history) == 1
    assert history[0]["id"] == "nvidia-spring"

    crawl()
    assert len(state.load("alert_history.json", [])) == 1


def test_failed_or_closed_existing_confirmation_never_creates_alert(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    state.save("jobs.json", {"nvidia-spring": _source_only_row()})
    monkeypatch.setattr("radar.quality.fetch_posting", lambda url: (None, ""))
    crawl()
    assert state.load("alert_history.json", []) == []


def test_fresh_ats_existing_refresh_promotes_once(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    row = _source_only_row()
    row["source"] = "greenhouse"
    monkeypatch.setattr(main, "_fetch_ats", lambda registry, disabled: ([_official_job()], {}, {"ats:greenhouse"}))
    state.save("jobs.json", {row["id"]: row})
    crawl()
    assert [r["id"] for r in state.load("alert_history.json", [])] == [row["id"]]


def test_prior_ready_and_prior_history_rows_do_not_realert(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    ready = _source_only_row()
    ready["alert_ok"] = True
    history = _source_only_row()
    history["id"] = "already-sent"
    history["url"] = "https://jobs.example.com/acme/careers/data-science-intern"
    history["title"] = "Data Science Intern"
    state.save("jobs.json", {ready["id"]: ready, history["id"]: history})
    state.save("alert_history.json", [{"id": history["id"], "alerted_at": NOW - 100}])
    monkeypatch.setattr("radar.quality.fetch_posting", lambda url: (True, _employer_text()))
    crawl()
    assert state.load("alert_history.json", []) == [{"id": history["id"], "alerted_at": NOW - 100}]


@pytest.mark.parametrize("field,value", [
    ("posted_at", NOW - 31 * 86400),
    ("posting_status", "expired"),
    ("manual_archived", True),
])
def test_stale_terminal_or_archived_existing_rows_do_not_alert(tmp_path, monkeypatch, field, value):
    _setup(tmp_path, monkeypatch)
    row = _source_only_row()
    row[field] = value
    state.save("jobs.json", {row["id"]: row})
    monkeypatch.setattr("radar.quality.fetch_posting", lambda url: (True, _employer_text()))
    crawl()
    assert state.load("alert_history.json", []) == []


def test_mixed_new_and_promoted_roles_share_cap_and_dedupe(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("RADAR_INTERNSHIP_MAX_ALERTS", "1")
    row = _source_only_row()
    new = _official_job("https://jobs.example.com/acme/careers/software-engineering-intern")
    monkeypatch.setattr(main, "_fetch_aggregators", lambda disabled: ([new], {}, {"aggregator:feed"}))
    state.save("jobs.json", {row["id"]: row})
    monkeypatch.setattr("radar.quality.fetch_posting", lambda url: (True, _employer_text()))
    crawl()
    history = state.load("alert_history.json", [])
    assert len(history) == 1
    assert len({entry["id"] for entry in history}) == 1
    rows = state.jobs()
    assert rows["nvidia-spring"]["alert_ok"] is True
    assert any(row.get("url", "").endswith("acme/careers/software-engineering-intern")
               and row.get("alert_ok") for row in rows.values())
