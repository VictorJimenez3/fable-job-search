import json
import time
from unittest.mock import patch

import pytest

from radar import state
from radar.applied import handle_event
from radar.board import _open_rows, _paginate
from radar.alerts import format_line

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
