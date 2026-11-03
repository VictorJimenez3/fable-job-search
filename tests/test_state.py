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
    assert "score_reasons" not in record
    assert record["detail_ref"]["version"] == 1
    detail = json.loads((tmp_path / record["detail_ref"]["shard"]).read_text())
    assert detail["records"]["job-1"]["score_dimensions"] == dimensions
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


def test_jobs_save_deduplicates_company_stage_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    state.save(
        "jobs.json",
        {
            "job-1": {
                "score_version": 13,
                "startup_stage": "late-stage startup",
                "startup_score": 12,
                "startup_stage_evidence": {
                    "value": "Series D company",
                    "confidence": "high",
                    "source_ids": ["src-1"],
                },
                "startup_stage_reason": "late-stage startup priority +12",
            }
        },
    )

    record = json.loads((tmp_path / "jobs.json").read_text())["job-1"]
    assert record["startup_stage"] == "late-stage startup"
    assert record["startup_score"] == 12
    assert "startup_stage_evidence" not in record
    assert "startup_stage_reason" not in record


def test_jobs_save_compacts_repeated_sponsorship_coverage(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    state.save("jobs.json", {"job-1": {
        "score_version": 13,
        "sponsorship_history": {
            "status": "likely", "coverage_quarters": ["2025-Q4"],
            "certified_cases": 3, "latest_decision_date": "2025-12-01",
        },
    }})
    record = json.loads((tmp_path / "jobs.json").read_text())["job-1"]
    assert record["sponsorship_history"] == {"status": "likely"}
    detail = json.loads((tmp_path / record["detail_ref"]["shard"]).read_text())["records"]["job-1"]
    assert detail["sponsorship_history"] == {
        "status": "likely", "certified_cases": 3, "latest_decision_date": "2025-12-01",
    }


def test_jobs_save_compacts_reconstructible_metadata_without_dropping_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    state.save("jobs.json", {"job-1": {
        "score_version": 13,
        "source": "simplify",
        "source_board": "simplify:new-grad",
        "source_variants": ["simplify", "jobright", "jobright"],
        "source_board_variants": ["simplify:new-grad", "jobright:software", "jobright:software"],
        "source_url": "https://example.test/feed",
        "source_url_variants": ["https://example.test/feed", "https://example.test/other"],
        "score_dimensions": {"base": 5, "role_fit": 20, "eligibility": 0, "mission": 0},
        "score_reasons": ["base utility +5", "role:swe +20"],
        "sponsorship_history": {
            "status": "likely", "certified_cases": 3,
            "certified_withdrawn_cases": 0, "certified_workers": 3,
            "certified_withdrawn_workers": 0,
        },
    }})

    record = json.loads((tmp_path / "jobs.json").read_text())["job-1"]
    detail = json.loads((tmp_path / record["detail_ref"]["shard"]).read_text())
    assert detail["records"]["job-1"]["score_dimensions"] == {"base": 5, "role_fit": 20}
    assert "score_reasons" not in record
    detail = json.loads((tmp_path / record["detail_ref"]["shard"]).read_text())
    assert detail["records"]["job-1"]["score_reasons"] == ["base utility +5", "role:swe +20"]
    assert detail["records"]["job-1"]["source_variants"] == ["jobright"]
    assert detail["records"]["job-1"]["source_board_variants"] == ["jobright:software"]
    assert detail["records"]["job-1"]["source_url_variants"] == ["https://example.test/other"]
    assert detail["records"]["job-1"]["sponsorship_history"] == {
        "status": "likely", "certified_cases": 3, "certified_workers": 3,
    }

    state.save("jobs.json", {"job-2": {
        "score_version": 13,
        "sponsorship_history": {
            "status": "likely", "certified_cases": 3,
            "certified_withdrawn_cases": 1, "certified_workers": 3,
            "certified_withdrawn_workers": 2,
        },
    }})
    retained = json.loads((tmp_path / "jobs.json").read_text())["job-2"]
    retained_detail = json.loads((tmp_path / retained["detail_ref"]["shard"]).read_text())["records"]["job-2"]
    assert retained_detail["sponsorship_history"]["certified_withdrawn_cases"] == 1
    assert retained_detail["sponsorship_history"]["certified_withdrawn_workers"] == 2


def test_jobs_save_preserves_previous_snapshot_when_size_guard_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    target = tmp_path / "jobs.json"
    target.write_text('{"existing":true}\n')
    monkeypatch.setenv("RADAR_MAX_JOB_SNAPSHOT_BYTES", "32")

    with pytest.raises(ValueError, match="Compact or shard state"):
        state.save("jobs.json", {"job-1": {"company": "A company name that exceeds the limit"}})

    assert target.read_text() == '{"existing":true}\n'
    assert not (tmp_path / "jobs.tmp").exists()


def test_scored_job_reasons_are_sharded_with_a_pointer_and_hydrated(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    row = {
        "id": "job-1",
        "company": "Acme",
        "title": "Software Engineer",
        "score_version": 15,
        "posting_status": "open",
        "score": 82,
        "score_reasons": ["role:swe +20", "new-grad/early-career priority +10"],
    }

    state.save("jobs.json", {"job-1": row})

    primary = json.loads((tmp_path / "jobs.json").read_text())
    saved = primary["job-1"]
    assert "score_reasons" not in saved
    assert saved["detail_ref"]["version"] == 1
    assert saved["detail_ref"]["shard"].startswith("jobs_details/")
    assert saved["detail_ref"]["key"] == "job-1"
    detail_path = tmp_path / saved["detail_ref"]["shard"]
    detail = json.loads(detail_path.read_text())
    assert detail["schema_version"] == 1
    assert detail["records"]["job-1"]["score_reasons"] == row["score_reasons"]
    assert detail["records"]["job-1"]["score_dimensions"] == {}
    assert detail["signatures"]["job-1"] == saved["detail_ref"]["sha256"]
    loaded = state.load("jobs.json", {})["job-1"]
    assert loaded["score_reasons"] == row["score_reasons"]
    assert "detail_ref" not in loaded
    assert state.load_job_snapshot(tmp_path / "jobs.json", {})["job-1"]["score_reasons"] == row["score_reasons"]


def test_open_eligibility_evidence_moves_to_detail_shard_and_hydrates(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    row = {
        "id": "job-1",
        "score_version": 16,
        "posting_status": "open",
        "score": 82,
        "eligibility": "eligible",
        "explicit_new_grad": True,
        "score_reasons": ["new-grad/early-career priority +10"],
        "eligibility_evidence": {
            "status": "verified",
            "source": "description",
            "required_floor": 0,
            "preferred_floor": 2,
            "experience_conflict": False,
        },
    }

    state.save("jobs.json", {"job-1": row})

    primary = json.loads((tmp_path / "jobs.json").read_text())["job-1"]
    detail = json.loads((tmp_path / primary["detail_ref"]["shard"]).read_text())
    assert "eligibility_evidence" not in primary
    assert detail["records"]["job-1"]["eligibility_evidence"] == row["eligibility_evidence"]
    assert state.load("jobs.json", {})["job-1"]["eligibility_evidence"] == row["eligibility_evidence"]


def test_compact_index_preserves_hard_gate_reasons_for_ui_filtering(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    row = {
        "id": "senior-role",
        "company": "Acme",
        "title": "Senior Software Engineer",
        "score_version": 16,
        "posting_status": "open",
        "score": 35,
        "score_reasons": [
            "base utility: 40",
            "senior+ title",
            "re-gate v16: requires 5+ years (not new-grad)",
        ],
    }

    state.save("jobs.json", {row["id"]: row})

    primary = json.loads((tmp_path / "jobs.json").read_text())[row["id"]]
    assert primary["gating_summary"]["reasons"] == [
        "senior+ title",
        "re-gate v16: requires 5+ years (not new-grad)",
    ]
    detail = json.loads((tmp_path / primary["detail_ref"]["shard"]).read_text())
    assert detail["records"][row["id"]]["score_reasons"] == row["score_reasons"]
    assert state.load("jobs.json", {})[row["id"]]["score_reasons"] == row["score_reasons"]
    state.save("jobs.json", {row["id"]: primary})
    compact_again = json.loads((tmp_path / "jobs.json").read_text())[row["id"]]
    assert compact_again["gating_summary"]["reasons"] == primary["gating_summary"]["reasons"]


def test_partial_rebuild_preserves_other_detail_fields_from_a_valid_pointer(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    row = {
        "id": "job-1",
        "score_version": 16,
        "posting_status": "open",
        "score_reasons": ["original"],
        "eligibility_evidence": {"status": "verified", "source": "description"},
    }
    state.save("jobs.json", {"job-1": row})
    primary = json.loads((tmp_path / "jobs.json").read_text())
    primary["job-1"]["score_reasons"] = ["rebuilt"]

    state.save("jobs.json", primary)

    loaded = state.load("jobs.json", {})["job-1"]
    assert loaded["score_reasons"] == ["rebuilt"]
    assert loaded["eligibility_evidence"] == row["eligibility_evidence"]


def test_drawer_audit_fields_are_sharded_with_filter_hints_in_primary(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    row = {
        "id": "job-1",
        "score_version": 16,
        "posting_status": "open",
        "score_reasons": ["role:swe +20"],
        "score_dimensions": {"role_fit": 20},
        "sponsorship_history": {
            "status": "likely", "certified_cases": 3,
            "latest_decision_date": "2026-01-01",
        },
        "link_resolution": {
            "status": "resolved", "original_url": "https://aggregator.test/job",
        },
        "posting_identity": {"variant_count": 2, "family_id": "family-1"},
        "source_variants": ["simplify", "jobright"],
        "source_board_variants": ["simplify:new-grad"],
        "source_url_variants": ["https://simplify.test/feed"],
        "lifecycle_events": [{"status": "open", "at": 10}],
        "posting": {
            "sponsorship": "yes", "years_min": 4, "intern_counts": True,
            "sponsorship_note": "full detail stays in the drawer shard",
        },
        "lifecycle_checked_at": 123,
        "posting_status_reason": "source rechecked",
        "rules_v": 16,
        "score_raw": 54.5,
        "score_calibrated": 55,
        "evidence_score": 55,
        "ranking_adjustment": -2,
    }

    state.save("jobs.json", {"job-1": row})

    primary = json.loads((tmp_path / "jobs.json").read_text())["job-1"]
    detail = json.loads((tmp_path / primary["detail_ref"]["shard"]).read_text())["records"]["job-1"]
    assert primary["sponsorship_history"] == {"status": "likely"}
    assert primary["link_resolution"] == {"status": "resolved"}
    assert primary["posting_identity"] == {"variant_count": 2}
    assert primary["posting"] == {
        "sponsorship": "yes", "years_min": 4, "intern_counts": True,
    }
    for key in ("lifecycle_checked_at", "posting_status_reason", "rules_v", "score_raw",
                "score_calibrated", "ranking_adjustment"):
        assert key not in primary
    assert primary["evidence_score"] == 55
    assert "lifecycle_events" not in primary
    assert detail["sponsorship_history"]["certified_cases"] == 3
    assert detail["link_resolution"]["original_url"] == "https://aggregator.test/job"
    assert detail["posting_identity"]["family_id"] == "family-1"
    assert detail["posting"] == row["posting"]
    assert detail["lifecycle_checked_at"] == 123
    assert detail["posting_status_reason"] == "source rechecked"
    assert detail["rules_v"] == 16
    assert state.load("jobs.json", {})["job-1"]["posting"] == row["posting"]
    assert state.load("jobs.json", {})["job-1"]["lifecycle_events"] == row["lifecycle_events"]


def test_empty_posting_hint_stays_distinct_from_unread(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    row = {
        "id": "job-1", "company": "Acme", "title": "Engineer",
        "score_version": 16, "posting_status": "open",
        "score_reasons": ["role:swe +20"],
        "posting": {"sponsorship_note": "requires drawer detail"},
    }

    state.save("jobs.json", {"job-1": row})

    primary = json.loads((tmp_path / "jobs.json").read_text())["job-1"]
    assert primary["posting"] == {}
    detail = json.loads((tmp_path / primary["detail_ref"]["shard"]).read_text())
    assert detail["records"]["job-1"]["posting"] == row["posting"]
    assert state.load("jobs.json", {})["job-1"]["posting"] == row["posting"]


def test_detail_shard_has_its_own_size_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    monkeypatch.setenv("RADAR_MAX_JOB_DETAIL_SHARD_BYTES", "64")
    target = tmp_path / "jobs.json"
    target.write_text('{"existing":true}\n')
    row = {
        "id": "job-1",
        "score_version": 15,
        "score_reasons": ["reason " * 40],
    }

    with pytest.raises(ValueError, match="generated job detail shard"):
        state.save("jobs.json", {"job-1": row})

    assert target.read_text() == '{"existing":true}\n'


def test_detail_signature_rejects_a_mixed_revision(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    row = {
        "id": "job-1",
        "score_version": 15,
        "score_reasons": ["current evidence"],
    }
    state.save("jobs.json", {"job-1": row})
    primary = json.loads((tmp_path / "jobs.json").read_text())
    detail_path = tmp_path / primary["job-1"]["detail_ref"]["shard"]
    detail = json.loads(detail_path.read_text())
    detail["records"]["job-1"]["score_reasons"] = ["stale evidence"]
    detail_path.write_text(json.dumps(detail))

    loaded = state.load("jobs.json", {})["job-1"]
    assert "score_reasons" not in loaded
    assert loaded["detail_ref"] == primary["job-1"]["detail_ref"]


def test_save_rejects_a_compact_row_with_missing_or_mismatched_detail(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    row = {"id": "job-1", "score_version": 15, "score_reasons": ["current evidence"]}
    state.save("jobs.json", {"job-1": row})
    primary = json.loads((tmp_path / "jobs.json").read_text())
    primary["job-1"].pop("score_reasons", None)
    detail_path = tmp_path / primary["job-1"]["detail_ref"]["shard"]
    detail_path.unlink()
    before = (tmp_path / "jobs.json").read_text()

    with pytest.raises(ValueError, match="detail shard.*missing or checksum mismatch"):
        state.save("jobs.json", primary)

    assert (tmp_path / "jobs.json").read_text() == before


def test_save_rejects_rebuilt_row_when_terminal_detail_pointer_is_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    original = {
        "id": "closed",
        "company": "Acme",
        "title": "Engineer",
        "score_version": 15,
        "posting_status": "expired",
        "score": 70,
        "score_reasons": ["original evidence"],
        "posting": {"years_min": 0},
    }
    state.save("jobs.json", {"closed": original})
    primary_path = tmp_path / "jobs.json"
    detail_path = tmp_path / json.loads(primary_path.read_text())["closed"]["detail_ref"]["shard"]
    primary_before = primary_path.read_text()
    detail_path.write_text(json.dumps({
        "schema_version": 1,
        "records": {"closed": {"score_reasons": ["stale evidence"]}},
        "signatures": {"closed": "wrong"},
    }))
    rebuilt = json.loads(primary_before)
    rebuilt["closed"]["score_reasons"] = ["rebuild evidence"]

    with pytest.raises(ValueError, match="detail shard.*missing or checksum mismatch"):
        state.save("jobs.json", rebuilt)

    assert primary_path.read_text() == primary_before


def test_loader_does_not_apply_another_job_detail_to_this_primary_key(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    rows = {
        "job-1": {"id": "job-1", "score_version": 15, "score_reasons": ["one"]},
        "job-2": {"id": "job-2", "score_version": 15, "score_reasons": ["two"]},
    }
    state.save("jobs.json", rows)
    primary_path = tmp_path / "jobs.json"
    primary = json.loads(primary_path.read_text())
    primary["job-1"]["detail_ref"] = primary["job-2"]["detail_ref"]
    primary_path.write_text(json.dumps(primary))

    loaded = state.load("jobs.json", {})
    assert "score_reasons" not in loaded["job-1"]
    assert loaded["job-2"]["score_reasons"] == ["two"]


def test_terminal_job_reasons_use_detail_shard_while_legacy_history_still_hydrates(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    row = {
        "id": "closed",
        "company": "Acme",
        "title": "Software Engineer",
        "score_version": 15,
        "posting_status": "expired",
        "score": 70,
        "score_reasons": ["source closed"],
    }

    state.save("jobs.json", {"closed": row})

    primary = json.loads((tmp_path / "jobs.json").read_text())["closed"]
    assert "score_reasons" not in primary
    assert not (tmp_path / "jobs_history.json").exists()
    assert state.load("jobs.json", {})["closed"]["score_reasons"] == row["score_reasons"]

    # A pre-sharding checkout with only the legacy history file remains readable.
    primary.pop("detail_ref")
    (tmp_path / "jobs.json").write_text(json.dumps({"closed": primary}))
    (tmp_path / "jobs_history.json").write_text(json.dumps({"closed": row}))
    assert state.load_job_snapshot(tmp_path / "jobs.json", {})["closed"]["score_reasons"] == row["score_reasons"]


def test_terminal_job_reasons_are_sharded_and_restored(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    row = {
        "id": "job-1",
        "score_version": 13,
        "posting_status": "expired",
        "score": 72,
        "score_reasons": ["role fit +20", "startup +12"],
    }

    state.save("jobs.json", {"job-1": row})

    primary = json.loads((tmp_path / "jobs.json").read_text())
    assert "score_reasons" not in primary["job-1"]
    detail_path = tmp_path / primary["job-1"]["detail_ref"]["shard"]
    detail = json.loads(detail_path.read_text())
    assert detail["records"]["job-1"]["score_reasons"] == row["score_reasons"]
    assert state.load("jobs.json", {})["job-1"]["score_reasons"] == row["score_reasons"]


def test_terminal_job_detail_is_sharded_but_history_summary_stays_visible(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    row = {
        "id": "job-1", "company": "Acme", "title": "Engineer",
        "url": "https://example.test/job", "score_version": 13, "score": 72,
        "posting_status": "expired", "posting_status_reason": "source closed",
        "manual_archived": True, "archive_reason": "owner archived",
        "first_seen": 100, "last_seen_at": 200, "closed_at": 300,
        "locations": ["New York, NY"],
        "score_reasons": ["role fit +20"],
        "score_dimensions": {"role_fit": 20},
        "lifecycle_events": [
            {"status": "open", "at": 100},
            {"status": "seen", "at": 150},
            {"status": "updated", "at": 200},
            {"status": "stale", "at": 250},
            {"status": "expired", "at": 300, "reason": "source closed"},
        ],
        "posting": {"years_min": 0},
        "sponsorship_history": {"status": "likely", "certified_cases": 3},
        "link_resolution": {"checked_at": 250, "candidate": "https://example.test/job"},
    }
    state.save("jobs.json", {"job-1": row})

    primary = json.loads((tmp_path / "jobs.json").read_text())["job-1"]
    for key in ("id", "company", "title", "url", "score", "posting_status",
                "posting_status_reason", "manual_archived", "archive_reason",
                "first_seen", "last_seen_at", "closed_at", "locations"):
        assert primary[key] == row[key]
    assert primary["lifecycle_events"] == row["lifecycle_events"][-4:]
    for key in ("score_reasons", "score_dimensions",
                "posting", "sponsorship_history", "link_resolution"):
        assert key not in primary
        assert json.loads((tmp_path / primary["detail_ref"]["shard"]
                           ).read_text())["records"]["job-1"][key] == row[key]
        assert state.load("jobs.json", {})["job-1"][key] == row[key]


def test_stale_history_does_not_reclose_reopened_or_purged_postings(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    (tmp_path / "jobs.json").write_text(json.dumps({
        "reopened": {"id": "reopened", "posting_status": "open", "score": 80}
    }))
    (tmp_path / "jobs_history.json").write_text(json.dumps({
        "reopened": {"id": "reopened", "posting_status": "expired", "score": 20},
        "purged": {"id": "purged", "posting_status": "expired", "score": 30},
    }))
    assert state.load("jobs.json", {}) == {
        "reopened": {"id": "reopened", "posting_status": "open", "score": 80}
    }


def test_open_job_reasons_use_the_detail_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    row = {
        "id": "job-1",
        "score_version": 13,
        "posting_status": "open",
        "score_reasons": ["role fit +20"],
    }

    state.save("jobs.json", {"job-1": row})

    primary = json.loads((tmp_path / "jobs.json").read_text())
    assert "score_reasons" not in primary["job-1"]
    detail = json.loads((tmp_path / primary["job-1"]["detail_ref"]["shard"]).read_text())
    assert detail["records"]["job-1"]["score_reasons"] == row["score_reasons"]
    assert state.load("jobs.json", {})["job-1"]["score_reasons"] == row["score_reasons"]


def test_non_job_state_is_not_compacted(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    value = {"remote": False, "items": []}
    state.save("web_state.json", value)
    assert state.load("web_state.json", {}) == value


@pytest.mark.parametrize("namespace,prefix", [("new_grad", ""), ("internship", "intern_")])
def test_terminal_ranking_metadata_does_not_exhaust_primary_snapshot(tmp_path, monkeypatch, namespace, prefix):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    monkeypatch.setenv("RADAR_MAX_JOB_SNAPSHOT_BYTES", "600")
    row = {
        "id": "closed", "company": "Acme", "title": "Software Engineer",
        "url": "https://example.test/job", "score": 72, "score_version": 15,
        "posting_status": "expired", "posting_status_reason": "source closed",
        "posting_status_changed_at": 300, "closed_at": 300,
        "first_seen": 100, "last_seen_at": 200,
        "locations": ["New York, NY", "San Francisco, CA"],
        "sector": "healthtech", "source": "greenhouse",
        "source_board": "ats:greenhouse:acme", "ats": "greenhouse",
        "posted_at": 100, "remote": True,
        "rules_v": 15, "evidence_score": 74, "eligibility": "eligible",
        "priority_tier": "recommended", "career_priority": 3,
        "explicit_new_grad": True, "startup_stage": "late-stage startup",
        "startup_score": 12, "score_dimensions": {"role_fit": 20},
        "score_reasons": ["role:swe +20", "healthcare preference +6"],
    }

    state.save("jobs.json", {"closed": row}, namespace=namespace)

    primary_path = tmp_path / f"{prefix}jobs.json"
    primary = json.loads(primary_path.read_text())["closed"]
    assert primary_path.stat().st_size <= 600
    for key in ("id", "company", "title", "url", "score", "score_version",
                "posting_status", "posting_status_reason", "posting_status_changed_at",
                "closed_at", "first_seen", "last_seen_at", "locations", "posted_at"):
        assert primary[key] == row[key]
    for key in ("source_board", "eligibility", "evidence_score",
                "startup_stage", "score_reasons"):
        assert key not in primary
    assert state.load("jobs.json", {}, namespace=namespace) == {"closed": row}
    # A normal later writer must preserve the same complete record and format.
    state.save("jobs.json", state.load("jobs.json", {}, namespace=namespace), namespace=namespace)
    assert json.loads(primary_path.read_text())["closed"] == primary
    assert state.load("jobs.json", {}, namespace=namespace) == {"closed": row}


@pytest.mark.parametrize("filename", ["jobs.json", "intern_jobs.json"])
def test_path_based_job_reader_hydrates_only_matching_terminal_history(tmp_path, filename):
    primary = {
        "closed": {"posting_status": "expired", "posting_status_changed_at": 300, "score": 70},
        "reopened": {"posting_status": "open", "score": 80},
        "changed": {"posting_status": "expired", "posting_status_changed_at": 500},
    }
    full = {"posting_status": "expired", "posting_status_changed_at": 300,
            "sector": "healthtech", "source": "greenhouse", "score": 60,
            "score_reasons": ["role:swe +20"]}
    (tmp_path / filename).write_text(json.dumps(primary))
    history_name = filename.replace("jobs.json", "jobs_history.json")
    (tmp_path / history_name).write_text(json.dumps({
        "closed": full, "reopened": full, "changed": full, "purged": full,
    }))

    restored = state.load_job_snapshot(tmp_path / filename, {})

    assert restored["closed"] == {**full, **primary["closed"]}
    assert restored["reopened"] == primary["reopened"]
    assert restored["changed"] == primary["changed"]
    assert "purged" not in restored
