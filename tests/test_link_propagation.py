"""Regression tests for propagating resolved posting URLs into trackers."""

import pytest

from radar.applied import (
    promote_application_url,
    record_applied,
    refresh_application_urls,
)
from radar import notion_sync
from radar import main as radar_main
from radar import state
from radar.db.repository import _merge_external_links
from unittest.mock import patch


def _job(job_id: str, url: str, **extra) -> dict:
    value = {
        "id": job_id,
        "company": "Acme",
        "title": "Software Engineer",
        "url": url,
        "source": "greenhouse",
    }
    value.update(extra)
    return value


def test_promote_application_url_preserves_tracker_fields_and_old_url():
    old_url = "https://jobright.ai/jobs/info/legacy"
    new_url = "https://boards.greenhouse.io/acme/jobs/123"
    entry = {
        "id": "job-1",
        "url": old_url,
        "stage": "interview",
        "notes": "owner note",
        "applied_at": 123,
        "alternate_urls": ["https://example.test/other"],
    }

    assert promote_application_url(entry, _job("job-1", new_url)) is True
    assert entry["url"] == new_url
    assert old_url in entry["alternate_urls"]
    assert "https://example.test/other" in entry["alternate_urls"]
    assert entry["stage"] == "interview"
    assert entry["notes"] == "owner note"
    assert entry["applied_at"] == 123

    # A retry is a no-op and does not duplicate the saved alternative.
    assert promote_application_url(entry, _job("job-1", new_url)) is False
    assert entry["alternate_urls"].count(old_url) == 1


def test_promote_application_url_accepts_known_alternate_but_rejects_manual_url():
    old_url = "https://careers.acme.test/legacy-role"
    new_url = "https://jobs.lever.co/acme/123"
    entry = {"id": "job-2", "url": old_url, "notes": "keep"}
    assert promote_application_url(
        entry, _job("job-2", new_url, alternate_urls=[old_url])
    ) is True
    assert entry["url"] == new_url

    manual = {"id": "job-3", "url": "https://victor.example/notes", "stage": "saved"}
    assert promote_application_url(manual, _job("job-3", new_url)) is False
    assert manual["url"] == "https://victor.example/notes"
    assert "alternate_urls" not in manual


def test_source_only_curator_url_does_not_promote_tracker():
    entry = {"id": "job-curator", "url": "https://jobright.ai/jobs/info/curator"}
    job = _job(
        "job-curator",
        "https://jobs.lever.co/acme/curator",
        link_resolution={"status": "source_only", "source_note": "apply here"},
    )
    assert promote_application_url(entry, job) is False
    assert entry["url"] == "https://jobright.ai/jobs/info/curator"


def test_refresh_application_urls_only_changes_verified_job_matches():
    entries = [
        {"id": "job-1", "url": "https://jobright.ai/jobs/info/1"},
        {"id": "job-2", "url": "https://victor.example/notes"},
        {"id": "job-3", "url": "https://jobright.ai/jobs/info/3"},
    ]
    jobs = {
        "job-1": _job("job-1", "https://boards.greenhouse.io/acme/jobs/1"),
        "job-2": _job("job-2", "https://boards.greenhouse.io/acme/jobs/2"),
        "job-3": _job("job-3", "https://jobright.ai/jobs/info/3"),
    }

    assert refresh_application_urls(entries, jobs) == 1
    assert entries[0]["url"] == jobs["job-1"]["url"]
    assert entries[1]["url"] == "https://victor.example/notes"
    assert entries[2]["url"] == jobs["job-3"]["url"]


def test_record_applied_promotes_existing_aggregator_without_stage_change():
    entry = {
        "id": "job-4",
        "company": "Acme",
        "title": "Software Engineer",
        "url": "https://jobright.ai/jobs/info/4",
        "stage": "applied",
        "notes": "keep",
    }
    applied = [entry]
    feedback = {"company_boosts": {}, "token_boosts": {}}

    assert record_applied(
        _job("job-4", "https://jobs.lever.co/acme/4"),
        applied,
        feedback,
        via="retry",
        stage="applied",
    ) is True
    assert entry["url"] == "https://jobs.lever.co/acme/4"
    assert entry["stage"] == "applied"
    assert entry["notes"] == "keep"


def test_notion_url_refresh_is_url_only_and_receipted(monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", "token")
    page = "https://app.notion.com/p/Acme-3995d6f42cab81b79291edce7b1639b2"
    entry = {
        "id": "job-5", "company": "Acme", "title": "Engineer",
        "url": "https://boards.greenhouse.io/acme/jobs/5",
        "notion_url": "https://jobright.ai/jobs/info/5",
        "notion_page": page, "notion_synced": True,
        "notion_stage": "saved", "stage": "saved",
    }
    schema = {"id": "db", "properties": {"Company": "title", "Stage": "status", "Job URL": "url"}}
    with patch.object(notion_sync, "resolve_database", return_value=schema), \
         patch.object(notion_sync.requests, "patch") as patch_request:
        patch_request.return_value.raise_for_status = lambda: None
        assert notion_sync.sync_applied([entry]) == 1
    props = patch_request.call_args.kwargs["json"]["properties"]
    assert props == {"Job URL": {"url": entry["url"]}}
    assert entry["notion_url"] == entry["url"]


def test_notion_url_refresh_skips_database_without_url_property(monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", "token")
    entry = {
        "id": "job-6", "company": "Acme", "title": "Engineer",
        "url": "https://boards.greenhouse.io/acme/jobs/6",
        "notion_url": "https://jobright.ai/jobs/info/6",
        "notion_page": "https://app.notion.com/p/Acme-3995d6f42cab81b79291edce7b1639b2",
        "notion_synced": True, "notion_stage": "saved", "stage": "saved",
    }
    schema = {"id": "db", "properties": {"Company": "title", "Stage": "status"}}
    with patch.object(notion_sync, "resolve_database", return_value=schema), \
         patch.object(notion_sync.requests, "patch") as patch_request:
        assert notion_sync.sync_applied([entry]) == 0
        patch_request.assert_not_called()
    assert entry["notion_url"] != entry["url"]


def test_notion_remote_url_edit_becomes_owner_override_and_is_not_replaced(monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", "token")
    page = "https://app.notion.com/p/Acme-3995d6f42cab81b79291edce7b1639b2"
    entry = {
        "id": "job-remote", "company": "Acme", "title": "Engineer",
        "url": "https://boards.greenhouse.io/acme/jobs/remote",
        "notion_url": "https://jobright.ai/jobs/info/remote",
        "notion_page": page, "notion_synced": True,
        "notion_stage": "saved", "stage": "saved",
    }
    schema = {"id": "db", "properties": {"Company": "title", "Stage": "status", "Job URL": "url"}}
    remote = "https://victor.example/manual-application-link"
    payload = {"results": [{"id": "3995d6f4-2cab-81b7-9291-edce7b1639b2",
                             "properties": {
                                 "Stage": {"status": {"name": "Not started"}},
                                 "Job URL": {"type": "url", "url": remote},
                             }}], "has_more": False}
    with patch.object(notion_sync, "resolve_database", return_value=schema), \
         patch.object(notion_sync.requests, "post") as query:
        query.return_value.raise_for_status = lambda: None
        query.return_value.json = lambda: payload
        assert notion_sync.sync_from_notion([entry]) == 1
    assert entry["notion_url"] == remote
    assert entry["notion_url_owner_override"] is True

    with patch.object(notion_sync, "resolve_database", return_value=schema), \
         patch.object(notion_sync.requests, "patch") as patch_request:
        assert notion_sync.sync_applied([entry]) == 0
        patch_request.assert_not_called()


def test_notion_legacy_blank_url_does_not_block_later_verified_promotion(monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", "token")
    entry = {
        "id": "job-legacy-blank", "company": "Acme", "title": "Engineer",
        "url": "https://jobright.ai/jobs/info/legacy-blank",
        "notion_page": "https://app.notion.com/p/Acme-3995d6f42cab81b79291edce7b1639b2",
        "notion_synced": True, "notion_stage": "saved", "stage": "saved",
    }
    schema = {"id": "db", "properties": {"Company": "title", "Stage": "status", "Job URL": "url"}}
    payload = {"results": [{"id": "3995d6f4-2cab-81b7-9291-edce7b1639b2",
                             "properties": {
                                 "Stage": {"status": {"name": "Not started"}},
                                 "Job URL": {"type": "url", "url": None},
                             }}], "has_more": False}
    with patch.object(notion_sync, "resolve_database", return_value=schema), \
         patch.object(notion_sync.requests, "post") as query:
        query.return_value.raise_for_status = lambda: None
        query.return_value.json = lambda: payload
        notion_sync.sync_from_notion([entry])
    assert "notion_url" not in entry
    assert not entry.get("notion_url_owner_override")

    assert promote_application_url(
        entry, _job("job-legacy-blank", "https://jobs.lever.co/acme/legacy-blank")
    )
    with patch.object(notion_sync, "resolve_database", return_value=schema), \
         patch.object(notion_sync.requests, "patch") as patch_request:
        patch_request.return_value.raise_for_status = lambda: None
        assert notion_sync.sync_applied([entry]) == 1
        assert patch_request.call_args.kwargs["json"]["properties"] == {
            "Job URL": {"url": entry["url"]}
        }


@pytest.mark.parametrize("command", ["notion_backfill", "tracker_sync"])
def test_tracker_backfills_refresh_urls_before_pushing(monkeypatch, command):
    applied = [{"id": "job-7", "url": "https://jobright.ai/jobs/info/7"}]
    jobs = {"job-7": {"id": "job-7", "url": "https://jobs.lever.co/acme/7"}}
    calls = []
    monkeypatch.setattr(state, "applied", lambda: applied)
    monkeypatch.setattr(state, "jobs", lambda: jobs)
    monkeypatch.setattr(state, "save", lambda *args, **kwargs: None)
    monkeypatch.setattr(radar_main.applied_mod, "autopilot_choices", lambda *args: [])
    monkeypatch.setattr(radar_main.applied_mod, "refresh_application_urls",
                        lambda entries, current_jobs: calls.append(("refresh", entries, current_jobs)) or 1)
    monkeypatch.setattr(notion_sync, "sync_from_notion",
                        lambda entries: calls.append(("pull", entries)) or 0)
    monkeypatch.setattr(notion_sync, "sync_applied",
                        lambda entries: calls.append(("push", entries)) or 1)
    monkeypatch.setattr(notion_sync, "archive_terminal_pages", lambda *args: 0)

    assert getattr(radar_main, command)() == 0
    assert [call[0] for call in calls] == ["pull", "refresh", "push"]
    assert calls[1][2] is jobs


def test_application_receipts_merge_without_erasing_existing_links():
    existing = {
        "notion_page_id": "page-1",
        "sheet_row": 12,
        "alternate_urls": ["https://jobright.ai/jobs/info/1"],
    }
    incoming = {
        "sheet_row": 13,
        "alternate_urls": [
            "https://jobright.ai/jobs/info/1?utm_source=feed",
            "https://jobs.lever.co/acme/1",
        ],
    }
    merged = _merge_external_links(existing, incoming)
    assert merged["notion_page_id"] == "page-1"
    assert merged["sheet_row"] == 13
    assert merged["alternate_urls"] == [
        "https://jobright.ai/jobs/info/1",
        "https://jobs.lever.co/acme/1",
    ]
    assert _merge_external_links(merged, {}) == merged
