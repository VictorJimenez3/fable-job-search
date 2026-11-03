from datetime import UTC, datetime

import pytest

import radar.main as radar_main
from radar import alerts


class Response:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body

    def raise_for_status(self):
        return None


def test_existing_alert_lookup_is_bounded_to_delivery_window(monkeypatch):
    seen = []

    def get(url, **kwargs):
        seen.append(kwargs["params"])
        return Response([{"body": "- [ ] role <!--radar:already-->"}])

    monkeypatch.setattr(alerts.requests, "get", get)
    since = 1_700_000_000
    assert alerts._existing_alert_ids("owner/repo", since=since) == {"already"}
    assert seen[0]["page"] == 1
    assert seen[0]["per_page"] == 100
    assert seen[0]["labels"] == "radar-alerts"
    assert seen[0]["since"] == datetime.fromtimestamp(since - 300, UTC).isoformat().replace("+00:00", "Z")


def test_marker_lookup_accepts_lane_labels(monkeypatch):
    seen = []

    def get(url, **kwargs):
        seen.append(kwargs["params"])
        return Response([{"body": "<!--radar:batch-id-->"}])

    monkeypatch.setattr(alerts.requests, "get", get)
    assert alerts._existing_marker_ids(
        "owner/repo", labels="radar-alerts,radar-new-grad,radar-email-batch"
    ) == {"batch-id"}
    assert seen[0]["labels"] == "radar-alerts,radar-new-grad,radar-email-batch"


def test_existing_alert_lookup_expands_persisted_aliases(monkeypatch):
    monkeypatch.setattr(
        alerts.state, "load",
        lambda name, default=None, namespace=None:
        {"old-id": "new-id"} if name == "job_aliases.json" else default,
    )
    monkeypatch.setattr(
        alerts.requests, "get",
        lambda *args, **kwargs: Response([{
            "body": "<!--radar:old-id-->", "labels": [], "title": "🎯 old role",
        }]),
    )

    assert alerts._existing_alert_ids("owner/repo") == {"old-id", "new-id"}


def test_legacy_unlabelled_internship_marker_does_not_suppress_new_grad(monkeypatch):
    monkeypatch.setenv("RADAR_PROFILE", "new_grad")
    monkeypatch.setattr(
        alerts.requests, "get",
        lambda *args, **kwargs: Response([{
            "body": "Internship roles appear in the separate Internship lane.\n<!--radar:intern-->",
            "labels": [], "title": "🎓 Internship — role",
        }]),
    )

    assert alerts._existing_alert_ids("owner/repo") == set()


def test_legacy_unlabelled_internship_marker_is_kept_in_internship_lane(monkeypatch):
    monkeypatch.setenv("RADAR_PROFILE", "internship")
    monkeypatch.setattr(
        alerts.requests, "get",
        lambda *args, **kwargs: Response([{
            "body": "Internship roles appear in the separate Internship lane.\n<!--radar:intern-->",
            "labels": [], "title": "🎓 Internship — role",
        }]),
    )

    assert alerts._existing_alert_ids("owner/repo") == {"intern"}


def test_post_alerts_uses_oldest_recent_alert_as_idempotency_window(monkeypatch):
    captured = {}
    def existing(repo, since):
        captured["since"] = since
        return set()
    monkeypatch.setattr(alerts, "_existing_alert_ids", existing)
    monkeypatch.setattr(alerts, "_headers", lambda: {})
    monkeypatch.setattr(alerts, "github_repo", lambda: "owner/repo")
    monkeypatch.setattr("radar.culture.load", lambda: {})
    monkeypatch.setattr(alerts.requests, "post", lambda *args, **kwargs: Response({"html_url": "https://example/1"}))
    monkeypatch.setattr(alerts, "format_line", lambda job, culture: "line")
    monkeypatch.setattr(alerts, "env", lambda key: "token")
    jobs = [
        {"id": "a", "company": "Acme", "title": "AI Engineer", "alerted_at": 20},
        {"id": "b", "company": "Beta", "title": "Software Engineer", "alerted_at": 10},
    ]
    alerts.post_alerts(jobs)
    assert captured["since"] == 10


def test_post_alerts_collapses_repeated_history_aliases(monkeypatch):
    monkeypatch.setattr(alerts, "_existing_alert_ids", lambda *args, **kwargs: set())
    monkeypatch.setattr(alerts, "_headers", lambda: {})
    monkeypatch.setattr(alerts, "github_repo", lambda: "owner/repo")
    monkeypatch.setattr("radar.culture.load", lambda: {})
    monkeypatch.setattr(alerts, "format_line", lambda job, culture: "line")
    monkeypatch.setattr(alerts, "env", lambda key: "token")
    response = Response({"html_url": "https://example/1"})
    posts = []
    monkeypatch.setattr(
        alerts.requests, "post", lambda *args, **kwargs: posts.append(kwargs["json"]) or response
    )
    job = {"id": "same", "company": "Acme", "title": "AI Engineer", "alerted_at": 20}

    alerts.post_alerts([job, {**job, "alerted_at": 10}])

    assert len(posts) == 1


def test_post_alerts_dedupes_provider_url_variants_but_not_requisitions(monkeypatch):
    monkeypatch.setattr(alerts, "_existing_alert_ids", lambda *args, **kwargs: set())
    monkeypatch.setattr(alerts, "_headers", lambda: {})
    monkeypatch.setattr(alerts, "github_repo", lambda: "owner/repo")
    monkeypatch.setattr("radar.culture.load", lambda: {})
    monkeypatch.setattr(alerts, "format_line", lambda job, culture: "line")
    monkeypatch.setattr(alerts, "env", lambda key: "token")
    response = Response({"html_url": "https://example/1"})
    posts = []
    monkeypatch.setattr(
        alerts.requests, "post", lambda *args, **kwargs: posts.append(kwargs["json"]) or response
    )
    rows = [
        {"id": "detail", "company": "Acme", "title": "AI Engineer",
         "url": "https://jobs.lever.co/acme/role-1", "alerted_at": 20},
        {"id": "apply", "company": "Acme", "title": "AI Engineer",
         "url": "https://jobs.lever.co/acme/role-1/application", "alerted_at": 30},
        {"id": "other", "company": "Acme", "title": "AI Engineer",
         "url": "https://jobs.lever.co/acme/role-2", "alerted_at": 40},
    ]

    alerts.post_alerts(rows)

    assert len(posts) == 2


def test_post_alerts_revalidates_current_false_eligibility(monkeypatch):
    monkeypatch.setattr(alerts, "_existing_alert_ids", lambda *args, **kwargs: set())
    monkeypatch.setattr(alerts, "_headers", lambda: {})
    monkeypatch.setattr(alerts, "github_repo", lambda: "owner/repo")
    monkeypatch.setattr("radar.culture.load", lambda: {})
    monkeypatch.setattr(alerts, "format_line", lambda job, culture: "line")
    monkeypatch.setattr(alerts, "env", lambda key: "token")
    monkeypatch.setattr(
        alerts.requests, "post", lambda *args, **kwargs: pytest.fail("ineligible row posted")
    )
    row = {"id": "stale", "company": "Acme", "title": "AI Engineer",
           "url": "https://example.test/stale", "alerted_at": 20, "score": 95,
           "alert_ok": True}

    assert alerts.post_alerts([row], jobs_state={
        "stale": {**row, "score": 40, "alert_ok": False, "posting_status": "open"}
    }) is None


def test_deliver_alerts_hydrates_current_score_and_eligibility(monkeypatch):
    now = 1_800_000_000
    current = {
        "id": "current", "company": "Acme", "title": "SWE",
        "url": "https://boards.greenhouse.io/acme/jobs/123",
        "alert_ok": True, "score": 90, "posting_status": "open",
    }
    stale_or_demoted = {
        "id": "demoted", "company": "Beta", "title": "SWE",
        "url": "https://boards.greenhouse.io/beta/jobs/456",
        "alert_ok": False, "score": 99, "posting_status": "open",
    }
    history = [
        {"id": "old", "alerted_at": now - 3600},
        {"id": "demoted", "alerted_at": now - 3600},
    ]
    captured = {}
    monkeypatch.setattr(radar_main.time, "time", lambda: now)
    monkeypatch.setattr(
        radar_main.state, "load",
        lambda name, default=None: (
            history if name == "alert_history.json" else
            {"old": "current"} if name == "job_aliases.json" else default
        ),
    )
    monkeypatch.setattr(radar_main.state, "jobs", lambda: {
        "current": current, "demoted": stale_or_demoted,
    })
    monkeypatch.setattr(radar_main.state, "applied", lambda: [])
    monkeypatch.setattr(
        "radar.alerts.post_alerts",
        lambda records, *, jobs_state: captured.setdefault("alerts", records) or "issue-url",
    )
    monkeypatch.setattr(
        "radar.board.update_master_board",
        lambda jobs, applied: captured.setdefault("board", True),
    )

    radar_main.deliver_alerts()

    assert [record["id"] for record in captured["alerts"]] == ["current"]
    assert captured["alerts"][0]["score"] == 90


def test_deliver_alerts_surfaces_posting_failure(monkeypatch):
    now = 1_800_000_000
    monkeypatch.setattr(radar_main.time, "time", lambda: now)
    monkeypatch.setattr(
        radar_main.state, "load",
        lambda name, default=None: [{"id": "current", "alerted_at": now - 1}]
        if name == "alert_history.json" else default,
    )
    monkeypatch.setattr(radar_main.state, "jobs", lambda: {
        "current": {"id": "current", "alert_ok": True, "score": 90,
                    "posting_status": "open"},
    })
    monkeypatch.setattr(radar_main.state, "applied", lambda: [])
    monkeypatch.setattr(
        "radar.alerts.post_alerts",
        lambda records: (_ for _ in ()).throw(RuntimeError("GitHub unavailable")),
    )
    monkeypatch.setattr("radar.board.update_master_board", lambda *args: None)

    with pytest.raises(RuntimeError, match="delivery failed"):
        radar_main.deliver_alerts()
