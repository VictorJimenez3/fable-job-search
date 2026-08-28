import json

import pytest

from radar import state


def test_jobs_save_omits_only_reconstructible_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    dimensions = {"base": 5, "role_fit": 10}
    state.save(
        "jobs.json",
        {
            "job-1": {
                "id": "job-1",
                "company": "Acme",
                "title": "Software Engineer",
                "score": 72,
                "score_version": 1,
                "score_dimensions": dimensions,
                "score_dimensions_raw": dict(dimensions),
                "score_reasons": ["base utility +5"],
                "description": "",
                "remote": False,
                "alert_ok": False,
                "posting_status": "open",
                "locations": [],
            }
        },
    )

    saved = json.loads((tmp_path / "jobs.json").read_text())
    record = saved["job-1"]
    assert record["score"] == 72
    assert record["score_reasons"] == ["base utility +5"]
    assert record["score_dimensions"] == dimensions
    assert "score_dimensions_raw" not in record
    assert "description" not in record
    assert "remote" not in record
    assert "alert_ok" not in record
    assert "posting_status" not in record
    assert "locations" not in record


def test_jobs_save_keeps_nondefault_and_disabled_dimension_values(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    state.save(
        "jobs.json",
        {
            "job-1": {
                "score_version": 1,
                "remote": True,
                "alert_ok": True,
                "posting_status": "expired",
                "score_dimensions": {"role_fit": 0},
                "score_dimensions_raw": {"role_fit": 20},
            }
        },
    )

    record = state.load("jobs.json", {})["job-1"]
    assert record["remote"] is True
    assert record["alert_ok"] is True
    assert record["posting_status"] == "expired"
    assert record["score_dimensions_raw"] == {"role_fit": 20}


def test_jobs_save_preserves_previous_snapshot_when_size_guard_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    target = tmp_path / "jobs.json"
    target.write_text('{"existing":true}\n')
    monkeypatch.setenv("RADAR_MAX_JOB_SNAPSHOT_BYTES", "32")

    with pytest.raises(ValueError, match="Compact or shard state"):
        state.save("jobs.json", {"job-1": {"company": "A company name that exceeds the limit"}})

    assert target.read_text() == '{"existing":true}\n'
    assert not (tmp_path / "jobs.tmp").exists()


def test_non_job_state_is_not_compacted(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    value = {"remote": False, "items": []}
    state.save("web_state.json", value)
    assert state.load("web_state.json", {}) == value
