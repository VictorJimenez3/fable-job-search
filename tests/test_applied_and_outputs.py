import json
import time
from unittest.mock import patch

import pytest

from radar import notion_sync as ns
from radar import state
from radar.alerts import format_line
from radar.applied import handle_event
from radar.main import promote_shortlist_applications
from radar.digest import render_dashboard, render_rss
from radar.notion_sync import build_payload


@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    return tmp_path


JOB = {"id": "a" * 16, "company": "Tempus", "title": "ML Engineer, New Grad",
       "url": "https://boards.greenhouse.io/tempus/jobs/1", "source": "greenhouse",
       "locations": ["Chicago, IL"], "posted_at": int(time.time()) - 3600,
       "score": 88, "sector": "healthtech", "alert_ok": True,
       "score_reasons": [], "salary": "", "remote": False, "ats": "greenhouse",
       "description": "", "llm_note": ""}


def test_format_line_roundtrips_job_id():
    line = format_line(JOB)
    assert "<!--radar:" + JOB["id"] + "-->" in line
    assert line.startswith("- [ ]")
    assert "🔥" in line  # score 88


def test_checkbox_event_tracks_job_as_saved(tmp_state, tmp_path, monkeypatch):
    # a checked box means "track this in Notion now, status = not applied yet";
    # Victor flips the status in Notion himself when he actually applies.
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    state.save("jobs.json", {JOB["id"]: JOB})
    body = format_line(JOB).replace("- [ ]", "- [x]")
    event = {"sender": {"login": "VictorJimenez3"},
             "issue": {"body": body, "labels": [{"name": "radar-alerts"}]}}
    ev = tmp_path / "event.json"
    ev.write_text(json.dumps(event))
    handle_event(str(ev))
    assert len(state.applied()) == 1
    assert state.applied()[0]["company"] == "Tempus"
    assert state.applied()[0]["via"] == "issue-checkbox"
    assert state.applied()[0]["stage"] == "saved"
    assert state.shortlist() == []
    fb = state.feedback()
    assert fb["company_boosts"].get("tempus")


def test_applied_signal_promotes_saved_entry(tmp_state):
    from radar.applied import record_applied
    applied, fb = [], {"company_boosts": {}, "token_boosts": {}}
    assert record_applied(JOB, applied, fb, via="issue-checkbox", stage="saved")
    # same saved signal again is a dupe...
    assert not record_applied(JOB, applied, fb, via="issue-checkbox", stage="saved")
    # ...but an applied signal upgrades the entry in place
    assert record_applied(JOB, applied, fb, via="comment")
    assert len(applied) == 1 and applied[0]["stage"] == "applied"
    assert not record_applied(JOB, applied, fb, via="comment")


def test_applied_comment_clears_from_shortlist(tmp_state, tmp_path, monkeypatch):
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    state.save("jobs.json", {JOB["id"]: JOB})
    state.save("shortlist.json", [{"id": JOB["id"], "company": "Tempus", "title": JOB["title"],
                                   "url": JOB["url"], "locations": JOB["locations"],
                                   "score": JOB["score"], "source": JOB["source"],
                                   "shortlisted_at": int(time.time())}])
    event = {"sender": {"login": "VictorJimenez3"}, "issue": {"number": 1},
             "comment": {"body": f"applied {JOB['url']}"}}
    ev = tmp_path / "event.json"
    ev.write_text(json.dumps(event))
    handle_event(str(ev))
    assert len(state.applied()) == 1
    assert state.shortlist() == []  # promoted out of the shortlist


def test_promote_shortlist_migrates_old_checkbox_selections(tmp_state, monkeypatch):
    state.save("shortlist.json", [{"id": JOB["id"], "company": JOB["company"],
                                   "title": JOB["title"], "url": JOB["url"],
                                   "locations": JOB["locations"], "score": JOB["score"],
                                   "source": JOB["source"]}])
    monkeypatch.setattr(ns, "sync_applied", lambda applied: 0)
    assert promote_shortlist_applications() == 0
    assert state.shortlist() == []
    assert state.applied()[0]["via"] == "checkbox-migration"
    assert state.applied()[0]["stage"] == "saved"  # Victor applies in Notion, not here


def test_bot_events_ignored(tmp_state, tmp_path):
    state.save("jobs.json", {JOB["id"]: JOB})
    body = format_line(JOB).replace("- [ ]", "- [x]")
    event = {"sender": {"login": "github-actions[bot]"},
             "issue": {"body": body, "labels": [{"name": "radar-alerts"}]}}
    ev = tmp_path / "event.json"
    ev.write_text(json.dumps(event))
    handle_event(str(ev))
    assert state.applied() == []
    assert state.shortlist() == []


def test_skip_comment_downranks(tmp_state, tmp_path, monkeypatch):
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    state.save("jobs.json", {JOB["id"]: JOB})
    event = {"sender": {"login": "VictorJimenez3"},
             "issue": {"number": 1},
             "comment": {"body": f"skip {JOB['id']}\nskip DV Trading"}}
    ev = tmp_path / "event.json"
    ev.write_text(json.dumps(event))
    handle_event(str(ev))
    fb = state.feedback()
    assert "tempus" in fb["negative_companies"]          # by job id
    assert "dv trading" in fb["negative_companies"]      # by company name


FULL_SCHEMA = {"id": "db-full-id", "title": "2026 Applications", "properties": {
    "Company": "title", "Stage": "status", "Position": "multi_select",
    "Apply date": "date", "Text": "rich_text", "Job URL": "url", "Location": "rich_text",
}}


def test_notion_payload_matches_full_schema():
    p = build_payload(JOB, FULL_SCHEMA)
    assert p["parent"]["database_id"] == "db-full-id"
    props = p["properties"]
    assert props["Company"]["title"][0]["text"]["content"] == "Tempus"
    assert props["Stage"]["status"]["name"] == "Applied"
    # ML title must map onto the existing multi-select option (with its typo)
    assert props["Position"]["multi_select"][0]["name"] in {
        "AI/ML Software Engineer", "Machine Learning Enginner", "Software Engineer"}
    assert props["Job URL"]["url"].startswith("https://")
    assert "Apply date" in props


def test_notion_payload_saved_stage():
    from radar.config import profile
    want = profile()["notion"]["stage_saved"]  # "Waiting for a referral" per live schema
    saved = {**JOB, "stage": "saved"}
    # database whose Stage options include the configured saved status
    schema = {**FULL_SCHEMA, "status_options": {"Stage": [want, "Applied", "Interview"]}}
    p = build_payload(saved, schema)
    assert p["properties"]["Stage"]["status"]["name"] == want
    assert "Apply date" not in p["properties"]  # no apply date until applied
    # database without that option: omit Stage so Notion applies its default
    schema = {**FULL_SCHEMA, "status_options": {"Stage": ["Applied", "Interview"]}}
    p = build_payload(saved, schema)
    assert "Stage" not in p["properties"]


def test_sync_patches_stage_when_saved_entry_becomes_applied(tmp_state, monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", "tok")
    entry = {"id": JOB["id"], "company": "Tempus", "title": JOB["title"], "url": JOB["url"],
             "stage": "applied", "notion_synced": True, "notion_stage": "saved",
             "notion_page": "https://app.notion.com/p/Tempus-3995d6f42cab81b79291edce7b1639b2"}
    with patch.object(ns, "resolve_database", return_value=FULL_SCHEMA), \
         patch.object(ns.requests, "patch") as mock_patch:
        mock_patch.return_value.raise_for_status = lambda: None
        assert ns.sync_applied([entry]) == 1
    assert entry["notion_stage"] == "applied"
    sent = mock_patch.call_args.kwargs["json"]["properties"]
    assert sent["Stage"]["status"]["name"] == "Applied"
    assert "Apply date" in sent


def test_sync_patches_rejection_stage_with_response_date(tmp_state, monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", "tok")
    schema = {**FULL_SCHEMA, "properties": {**FULL_SCHEMA["properties"], "Response date": "date"},
              "status_options": {"Stage": ["Applied", "Interview", "OA", "Rejected", "CLOSED"]}}
    entry = {"id": JOB["id"], "company": "Stripe", "title": "SWE", "url": "u",
             "stage": "rejected", "notion_synced": True, "notion_stage": "applied",
             "responded_at": 1783000000,
             "notion_page": "https://app.notion.com/p/Stripe-3995d6f42cab81b79291edce7b1639b2"}
    with patch.object(ns, "resolve_database", return_value=schema), \
         patch.object(ns.requests, "patch") as mock_patch:
        mock_patch.return_value.raise_for_status = lambda: None
        assert ns.sync_applied([entry]) == 1
    assert entry["notion_stage"] == "rejected"
    sent = mock_patch.call_args.kwargs["json"]["properties"]
    assert sent["Stage"]["status"]["name"] == "Rejected"
    assert "Response date" in sent and "Apply date" not in sent


def test_sync_skips_stage_not_in_schema_options(tmp_state, monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", "tok")
    # a database lacking the "OA" option must not receive an invalid patch
    schema = {**FULL_SCHEMA, "status_options": {"Stage": ["Applied", "Rejected"]}}
    entry = {"id": JOB["id"], "company": "Stripe", "title": "SWE", "url": "u",
             "stage": "oa", "notion_synced": True, "notion_stage": "applied",
             "notion_page": "https://app.notion.com/p/Stripe-3995d6f42cab81b79291edce7b1639b2"}
    with patch.object(ns, "resolve_database", return_value=schema), \
         patch.object(ns.requests, "patch") as mock_patch:
        assert ns.sync_applied([entry]) == 0
        mock_patch.assert_not_called()
    assert entry["notion_stage"] == "applied"  # unchanged


def test_notion_payload_degrades_on_minimal_schema():
    # a "fresh slate" database might only have the two essentials — nothing
    # should crash, and fields absent from the schema are simply skipped
    minimal = {"id": "db-min", "title": "Fresh Slate",
               "properties": {"Company": "title", "Status": "status"}}
    p = build_payload(JOB, minimal)
    props = p["properties"]
    assert props["Status"]["status"]["name"] == "Applied"
    assert set(props) == {"Company", "Status"}


def test_resolve_database_finds_by_title_and_prefers_most_recent(tmp_state):
    search_result = {"results": [
        {"id": "old-id", "title": [{"plain_text": "Applications"}],
         "last_edited_time": "2025-01-01T00:00:00Z", "object": "database"},
        {"id": "new-id", "title": [{"plain_text": "2026 Applications"}],
         "last_edited_time": "2026-07-01T00:00:00Z", "object": "database"},
    ]}
    with patch.object(ns.requests, "post") as mock_post:
        mock_post.return_value.raise_for_status = lambda: None
        mock_post.return_value.json = lambda: search_result
        schema = ns.resolve_database("tok")
    assert schema["id"] == "new-id"
    assert state.load("notion_db.json", {})["id"] == "new-id"


def test_resolve_database_self_heals_when_cached_id_dies(tmp_state):
    state.save("notion_db.json", {"id": "dead-id", "title": "old"})
    search_result = {"results": [
        {"id": "fresh-id", "title": [{"plain_text": "2026 Applications"}],
         "last_edited_time": "2026-07-01T00:00:00Z", "object": "database"},
    ]}
    with patch.object(ns.requests, "get") as mock_get, patch.object(ns.requests, "post") as mock_post:
        mock_get.return_value.status_code = 404
        mock_post.return_value.raise_for_status = lambda: None
        mock_post.return_value.json = lambda: search_result
        schema = ns.resolve_database("tok")
    assert schema["id"] == "fresh-id"


def test_dashboard_and_rss_render():
    jobs = {JOB["id"]: {**JOB, "first_seen": int(time.time())}}
    registry = {"greenhouse:tempus": {"status": "active", "name": "Tempus"}}
    runs = [{"ts": int(time.time()), "new_jobs": 1, "alerts": 1}]
    md = render_dashboard(jobs, registry, runs)
    assert "Tempus" in md and "| 88" in md
    rss = render_rss([{**JOB, "alerted_at": int(time.time())}])
    assert "<rss" in rss and "Tempus" in rss and JOB["id"] in rss


def test_strategist_memo_builds(tmp_state, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    now = int(time.time())
    state.save("jobs.json", {JOB["id"]: {**JOB, "first_seen": now}})
    state.save("alert_history.json", [{**JOB, "alerted_at": now - 3600}])
    state.save("applied.json", [{"id": "b" * 16, "company": "Notion", "title": "SWE New Grad",
                                 "applied_at": now - 6 * 86400, "notion_synced": True}])
    from radar.strategist import build_memo
    memo = build_memo()
    assert "Pipeline" in memo and "1 alerts" in memo and "1 applications" in memo
    assert "Follow up" in memo and "Notion" in memo          # 6-day-old application nudged
    assert "Tempus" in memo                                   # top open target listed


def test_page_id_from_url_extracts_and_dashes():
    from radar.notion_sync import page_id_from_url
    url = "https://app.notion.com/p/Uber-Technologies-Inc-3995d6f42cab81b79291edce7b1639b2"
    assert page_id_from_url(url) == "3995d6f4-2cab-81b7-9291-edce7b1639b2"
    assert page_id_from_url("not a url") is None
