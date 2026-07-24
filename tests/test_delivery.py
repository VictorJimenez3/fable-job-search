from datetime import datetime, timezone

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
    assert seen[0]["since"] == datetime.fromtimestamp(since - 300, timezone.utc).isoformat().replace("+00:00", "Z")


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
