import json
import time

import pytest

from radar import state
from radar.alerts import format_line
from radar.applied import handle_event
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


def test_checkbox_event_records_applied(tmp_state, tmp_path, monkeypatch):
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    state.save("jobs.json", {JOB["id"]: JOB})
    body = format_line(JOB).replace("- [ ]", "- [x]")
    event = {"sender": {"login": "VictorJimenez3"},
             "issue": {"body": body, "labels": [{"name": "radar-alerts"}]}}
    ev = tmp_path / "event.json"
    ev.write_text(json.dumps(event))
    handle_event(str(ev))
    applied = state.applied()
    assert len(applied) == 1
    assert applied[0]["company"] == "Tempus"
    assert applied[0]["notion_synced"] is False  # queued until NOTION_TOKEN exists
    fb = state.feedback()
    assert fb["company_boosts"].get("tempus")


def test_bot_events_ignored(tmp_state, tmp_path):
    state.save("jobs.json", {JOB["id"]: JOB})
    body = format_line(JOB).replace("- [ ]", "- [x]")
    event = {"sender": {"login": "github-actions[bot]"},
             "issue": {"body": body, "labels": [{"name": "radar-alerts"}]}}
    ev = tmp_path / "event.json"
    ev.write_text(json.dumps(event))
    handle_event(str(ev))
    assert state.applied() == []


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


def test_notion_payload_matches_tracker_schema():
    p = build_payload(JOB)
    assert p["parent"]["database_id"] == "2205d6f42cab8139a20af375dc2923e6"
    props = p["properties"]
    assert props["Company"]["title"][0]["text"]["content"] == "Tempus"
    assert props["Stage"]["status"]["name"] == "Applied"
    # ML title must map onto the existing multi-select option (with its typo)
    assert props["Position"]["multi_select"][0]["name"] in {
        "AI/ML Software Engineer", "Machine Learning Enginner", "Software Engineer"}
    assert props["Job URL"]["url"].startswith("https://")
    assert "Apply date" in props


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
