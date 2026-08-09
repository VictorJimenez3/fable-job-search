import time

from radar import lifecycle
from radar import notion_sync
from radar.digest import render_dashboard, render_rss
from radar.models import Job


NOW = int(time.time())


def test_dead_page_classifies_filled_before_generic_expired():
    assert lifecycle.status_from_dead_text("This position has been filled.") == lifecycle.FILLED
    assert lifecycle.status_from_dead_text("This posting has expired.") == lifecycle.EXPIRED
    assert lifecycle.status_from_dead_text("") == lifecycle.EXPIRED


def test_terminal_state_is_auditable_and_idempotent():
    record = {"id": "x", "alert_ok": True, "score_reasons": []}
    assert lifecycle.mark_terminal(record, lifecycle.FILLED, NOW, "posting gone (link checked)")
    assert record["posting_status"] == "filled"
    assert record["alert_ok"] is False
    assert record["closed_at"] == NOW
    assert record["lifecycle_events"] == [{
        "status": "filled", "at": NOW, "reason": "posting gone (link checked)"}]
    assert "posting gone (link checked)" in record["score_reasons"]
    assert not lifecycle.mark_terminal(record, lifecycle.FILLED, NOW + 1, "same evidence")
    assert len(record["lifecycle_events"]) == 1


def test_reconcile_expires_unseen_rows_reopens_reappearing_rows(monkeypatch):
    monkeypatch.setenv("RADAR_LIFECYCLE_ACTIVE_DAYS", "45")
    monkeypatch.setenv("RADAR_LIFECYCLE_UNSEEN_GRACE_DAYS", "14")
    stale = {"id": "stale", "first_seen": NOW - 70 * 86400,
             "last_seen_at": NOW - 20 * 86400, "alert_ok": True, "score_reasons": []}
    seen = {"id": "seen", "first_seen": NOW - 70 * 86400,
            "last_seen_at": NOW - 20 * 86400, "alert_ok": True, "score_reasons": []}
    jobs = {"stale": stale, "seen": seen}
    stats = lifecycle.reconcile(jobs, NOW, {"seen"})
    assert stats["expired"] == 1
    assert stale["posting_status"] == "expired"
    assert lifecycle.status_of(seen) == "open"
    lifecycle.mark_terminal(seen, lifecycle.EXPIRED, NOW, "dead link")
    assert lifecycle.reconcile({"seen": seen}, NOW + 86400, {"seen"})["reopened"] == 1
    assert lifecycle.status_of(seen) == "open"


def test_reconcile_does_not_expire_on_an_all_source_outage(monkeypatch):
    monkeypatch.setenv("RADAR_LIFECYCLE_ACTIVE_DAYS", "45")
    monkeypatch.setenv("RADAR_LIFECYCLE_UNSEEN_GRACE_DAYS", "14")
    stale = {"id": "stale", "first_seen": NOW - 70 * 86400,
             "last_seen_at": NOW - 20 * 86400, "alert_ok": True, "score_reasons": []}
    stats = lifecycle.reconcile({"stale": stale}, NOW, set(), allow_source_gap_expiry=False)
    assert stats["expired"] == 0
    assert lifecycle.status_of(stale) == "open"
    assert not lifecycle.source_run_healthy({"simplify": "error: timeout"}, {"ok": 0, "failed": 12})
    assert lifecycle.source_run_healthy({"simplify": 0}, {"ok": 0, "failed": 12})


def test_manual_archive_stays_terminal_even_if_source_reappears():
    record = {"id": "manual", "manual_archived": True, "closed_at": NOW,
              "archive_reason": "filled", "alert_ok": False, "score_reasons": []}
    lifecycle.normalize_record(record, NOW)
    assert lifecycle.status_of(record) == lifecycle.FILLED
    assert lifecycle.reconcile({"manual": record}, NOW + 86400, {"manual"})["reopened"] == 0
    assert lifecycle.is_terminal(record)


def test_job_record_persists_lifecycle_fields():
    job = Job(company="Acme", title="Software Engineer", url="https://example.test/1",
              source="ats", locations=["Remote"])
    lifecycle.mark_terminal(job, lifecycle.EXPIRED, NOW, "posting gone (link checked)")
    record = job.to_record()
    assert record["posting_status"] == "expired"
    assert record["closed_at"] == NOW
    assert record["lifecycle_events"][0]["status"] == "expired"


def test_terminal_owner_notion_page_is_soft_archived(monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", "secret")
    calls = []
    monkeypatch.setattr(notion_sync, "archive_page",
                        lambda token, page_id: calls.append((token, page_id)))
    page_hex = "3995d6f42cab81b79291edce7b1639b2"
    applied = [{"id": "filled", "company": "Acme", "notion_page":
                f"https://app.notion.com/p/Acme-{page_hex}"}]
    jobs = {"filled": {"posting_status": "filled", "closed_at": NOW,
                        "posting_status_reason": "position filled"}}
    assert notion_sync.archive_terminal_pages(applied, jobs) == 1
    assert calls == [("secret", "3995d6f4-2cab-81b7-9291-edce7b1639b2")]
    assert applied[0]["notion_archived"] is True
    assert notion_sync.archive_terminal_pages(applied, jobs) == 0


def test_terminal_rows_leave_active_dashboard_and_feed():
    base = {"company": "Acme", "title": "Software Engineer", "url": "https://example.test/1",
            "score": 99, "posted_at": NOW - 3600, "first_seen": NOW - 3600,
            "locations": ["Remote"], "sector": "tech", "source": "ats", "id": "open"}
    terminal = {**base, "id": "filled", "posting_status": "filled", "closed_at": NOW}
    dashboard = render_dashboard({"open": base, "filled": terminal}, {}, [])
    assert "https://example.test/1" in dashboard
    assert "filled" not in dashboard
    feed = render_rss([{
        **base, "id": "filled", "alerted_at": NOW, "posting_status": "filled"},
        {**base, "id": "open", "alerted_at": NOW}],
        {"filled": terminal, "open": base})
    assert feed.count("<item>") == 1
    assert "<guid isPermaLink=\"false\">open</guid>" in feed


def test_expired_alert_history_is_not_republished_when_state_is_pruned():
    from radar.board import email_batch_rows

    alert = {"id": "gone", "company": "Acme", "title": "Engineer",
             "score": 90, "alerted_at": NOW - 3600}
    assert render_rss([alert], {}).count("<item>") == 0
    assert email_batch_rows([alert], set(), NOW, jobs_state={}) == []
