from radar.reports import REPORT_THRESHOLD, distinct_reporters, record_report, render_report
from radar.taste import record_feedback, render_report as render_taste_report


def job():
    return {"id": "0123456789abcdef", "company": "Acme", "title": "Machine Learning Engineer, New Grad",
            "url": "https://acme.example/jobs/1"}


def test_explicit_positive_feedback_is_capped_and_idempotent():
    feedback = {"company_boosts": {}, "token_boosts": {}, "negative_companies": []}
    assert record_feedback(feedback, job(), "up", "both") is True
    assert record_feedback(feedback, job(), "up", "both") is False
    assert feedback["company_boosts"]["acme"] == 2
    assert feedback["token_boosts"]["machine"] == 1
    assert len(feedback["taste_events"]) == 1
    assert "Machine Learning Engineer" in render_taste_report(feedback)


def test_negative_feedback_only_changes_the_selected_signal():
    feedback = {"company_boosts": {}, "token_boosts": {}, "negative_companies": []}
    assert record_feedback(feedback, job(), "down", "role") is True
    assert feedback["negative_companies"] == []
    assert feedback["token_boosts"]["machine"] == -1
    assert record_feedback(feedback, job(), "down", "company") is True
    assert feedback["negative_companies"] == ["acme"]


def test_community_reports_count_distinct_github_users():
    reports = {}
    for user in ["alice", "bob", "carol"]:
        assert record_report(reports, job(), user, "expired", 10, "https://github.com/x/issues/10")
    entry = reports[job()["id"]]
    assert distinct_reporters(entry) == REPORT_THRESHOLD
    assert record_report(reports, job(), "ALICE", "expired", 11, "") is False
    text = render_report(reports)
    assert "3" in text
    assert "@alice" in text and "@carol" in text
