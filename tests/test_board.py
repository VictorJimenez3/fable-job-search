import json
import time
from unittest.mock import patch

import pytest

from radar import state
from radar.applied import handle_event
from radar.board import _open_rows, _paginate, email_batch_rows
from radar.alerts import format_line, post_alerts

NOW = int(time.time())

JOB = {"id": "b" * 16, "company": "Anthropic", "title": "Research Engineer",
       "url": "https://boards.greenhouse.io/anthropic/jobs/9", "source": "greenhouse",
       "locations": ["San Francisco, CA"], "posted_at": NOW - 3600, "first_seen": NOW - 3600,
       "score": 84, "sector": "ai_lab", "alert_ok": True,
       "score_reasons": [], "salary": "", "remote": False, "ats": "greenhouse",
       "description": "", "llm_note": ""}


@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    return tmp_path


def test_open_rows_filters_and_sorts():
    low = {**JOB, "id": "c" * 16, "score": 50}                      # below alert threshold
    stale = {**JOB, "id": "d" * 16, "first_seen": NOW - 40 * 86400}  # too old
    unalertable = {**JOB, "id": "e" * 16, "alert_ok": False}
    rows = _open_rows({j["id"]: j for j in (JOB, low, stale, unalertable)}, NOW)
    assert [r["id"] for r in rows] == [JOB["id"]]


def test_paginate_respects_page_limit():
    lines = [f"- [ ] job {i} " + "x" * 200 for i in range(100)]
    pages = _paginate(lines, limit=2000)
    assert all(len(p) <= 2000 for p in pages)
    assert sum(p.count("- [ ]") for p in pages) == 100
    # order preserved across page boundaries
    assert "job 0 " in pages[0] and "job 99 " in pages[-1]


def test_email_batch_prioritizes_score_then_recency():
    history = [
        {"id": "old-high", "score": 95, "alerted_at": NOW - 3600},
        {"id": "new-high", "score": 95, "alerted_at": NOW - 60},
        {"id": "new-low", "score": 70, "alerted_at": NOW - 10},
        {"id": "sent", "score": 100, "alerted_at": NOW - 60},
        {"id": "stale", "score": 100, "alerted_at": NOW - 15 * 86400},
    ]
    rows = email_batch_rows(history, {"sent"}, NOW, limit=3)
    assert [r["id"] for r in rows] == ["new-high", "old-high", "new-low"]


def test_email_batch_holds_one_normal_role_until_minimum_or_timeout(tmp_state, monkeypatch):
    from radar.board import post_email_batch
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("RADAR_EMAIL_BATCH_MIN", "3")
    monkeypatch.setenv("RADAR_EMAIL_BATCH_MAX_WAIT_HOURS", "12")
    monkeypatch.setattr("radar.board.requests.post", lambda *a, **k: pytest.fail("should hold"))
    assert post_email_batch([{
        **JOB, "alerted_at": NOW - 2 * 3600,
    }]) is None


def test_email_batch_sends_one_after_max_wait(tmp_state, monkeypatch):
    from radar.board import post_email_batch
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("RADAR_EMAIL_BATCH_MIN", "3")
    monkeypatch.setenv("RADAR_EMAIL_BATCH_MAX_WAIT_HOURS", "12")
    response = type("Response", (), {"raise_for_status": lambda self: None,
                                      "json": lambda self: {"html_url": "https://github.test/1"}})()
    monkeypatch.setattr("radar.board.requests.post", lambda *a, **k: response)
    assert post_email_batch([{**JOB, "alerted_at": NOW - 13 * 3600}]) == "https://github.test/1"


def test_checkbox_in_board_page_comment_tracks_job(tmp_state, tmp_path, monkeypatch):
    # master-board pages live in bot comments; ticking a box there arrives as
    # an issue_comment edit and must track the job like a body checkbox
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    state.save("jobs.json", {JOB["id"]: JOB})
    body = format_line(JOB).replace("- [ ]", "- [x]")
    event = {"sender": {"login": "VictorJimenez3"},
             "issue": {"number": 2, "labels": [{"name": "radar-alerts"},
                                               {"name": "radar-master"}]},
             "comment": {"body": f"**Page 2**\n\n{body}"}}
    ev = tmp_path / "event.json"
    ev.write_text(json.dumps(event))
    handle_event(str(ev))
    assert len(state.applied()) == 1
    assert state.applied()[0]["stage"] == "saved"


def test_web_action_track_records_saved(tmp_state, tmp_path, monkeypatch):
    from radar.main import web_action
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    state.save("jobs.json", {JOB["id"]: JOB})
    ev = tmp_path / "dispatch.json"
    ev.write_text(json.dumps({"client_payload": {"action": "track", "id": JOB["id"]}}))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(ev))
    assert web_action() == 0
    assert state.applied()[0]["stage"] == "saved"
    assert state.applied()[0]["via"] == "platform"
    # the same job marked applied from the site promotes in place
    ev.write_text(json.dumps({"client_payload": {"action": "applied", "id": JOB["id"]}}))
    assert web_action() == 0
    assert len(state.applied()) == 1
    assert state.applied()[0]["stage"] == "applied"


def test_events_from_strangers_are_ignored(tmp_state, tmp_path, monkeypatch):
    # public repo: anyone can comment `applied <url>` — only the owner counts
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    state.save("jobs.json", {JOB["id"]: JOB})
    event = {"sender": {"login": "random-stranger"},
             "issue": {"number": 9, "labels": [{"name": "radar-alerts"}]},
             "comment": {"body": f"applied {JOB['url']}\nskip Tempus"}}
    ev = tmp_path / "event.json"
    ev.write_text(json.dumps(event))
    handle_event(str(ev))
    assert state.applied() == []
    assert state.feedback().get("negative_companies", []) == []


def test_opened_issue_save_command_tracks_job(tmp_state, tmp_path, monkeypatch):
    # tokenless platform path: owner opens a prefilled issue "save <id>"
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    state.save("jobs.json", {JOB["id"]: JOB})
    event = {"sender": {"login": "VictorJimenez3"}, "action": "opened",
             "issue": {"number": 10, "labels": [], "body": f"save {JOB['id']}"}}
    ev = tmp_path / "event.json"
    ev.write_text(json.dumps(event))
    handle_event(str(ev))
    assert len(state.applied()) == 1
    assert state.applied()[0]["stage"] == "saved"
    assert state.applied()[0]["via"] == "issue-command"


def test_untrack_removes_in_house_entry_and_tombstones_it(tmp_state, tmp_path, monkeypatch):
    from radar.main import web_action
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    entry = {"id": JOB["id"], "company": JOB["company"], "title": JOB["title"],
             "url": JOB["url"], "stage": "saved"}
    state.save("applied.json", [entry])
    event = tmp_path / "dispatch.json"
    event.write_text(json.dumps({"client_payload": {"action": "untrack",
                                                     "id": JOB["id"],
                                                     "url": JOB["url"]}}))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    assert web_action() == 0
    assert state.applied() == []
    assert JOB["id"] in state.load("untracked.json", [])


def test_alerts_create_one_silent_issue_per_missing_job(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr("radar.alerts.github_repo", lambda: "VictorJimenez3/fable-job-search")
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"html_url": f"https://github.com/example/{len(calls)}"}

    def fake_post(url, **kwargs):
        calls.append((url, kwargs["json"]))
        return Response()

    class ExistingResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return []

    monkeypatch.setattr("radar.alerts.requests.get", lambda *args, **kwargs: ExistingResponse())
    monkeypatch.setattr("radar.alerts.requests.post", fake_post)
    jobs = [{**JOB, "id": "1" * 16, "company": "PlayStation"},
            {**JOB, "id": "2" * 16, "company": "Fanatics"}]
    post_alerts(jobs)
    assert len(calls) == 2
    assert all(c[1]["assignees"] == [] for c in calls)
    assert all("Job Radar alerts — week" not in c[1]["title"] for c in calls)


def test_alerts_skip_existing_marker_even_when_issue_is_closed(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr("radar.alerts.github_repo", lambda: "VictorJimenez3/fable-job-search")

    class ExistingResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return [{"state": "closed", "body": "tracked <!--radar:1111111111111111-->"}]

    monkeypatch.setattr("radar.alerts.requests.get", lambda *args, **kwargs: ExistingResponse())
    calls = []
    monkeypatch.setattr("radar.alerts.requests.post", lambda *args, **kwargs: calls.append(args))
    post_alerts([{**JOB, "id": "1" * 16, "company": "PlayStation"}])
    assert calls == []
