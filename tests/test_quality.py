import time

import pytest

from radar import quality


NOW = int(time.time())


def _rec(**over):
    rec = {
        "id": "abc123", "company": "Acme Health", "title": "Software Engineer",
        "url": "https://boards.example.com/jobs/1", "score": 80,
        "score_reasons": ["base 40", "role:swe +20"], "alert_ok": True,
        "first_seen": NOW - 86400, "posted_at": NOW - 86400,
    }
    rec.update(over)
    return rec


class FakeResp:
    def __init__(self, status=200, text=""):
        self.status_code = status
        self.text = text


def _posting(body: str) -> str:
    return f"<html><body><div class='job'>{body}</div></body></html>" + " pad" * 200


def test_dead_link_404_closes_job(monkeypatch):
    monkeypatch.setattr(quality.http, "get", lambda *a, **k: FakeResp(404))
    rec = _rec()
    assert quality.verify(rec)
    assert rec["alert_ok"] is False
    assert rec["closed_at"]
    assert rec["quality"]["live"] is False
    assert any("posting gone" in r for r in rec["score_reasons"])


def test_dead_phrase_on_200_page_closes_job(monkeypatch):
    monkeypatch.setattr(quality.http, "get",
                        lambda *a, **k: FakeResp(200, _posting("This job is no longer available.")))
    rec = _rec()
    assert quality.verify(rec)
    assert rec["alert_ok"] is False


def test_llm_not_new_grad_suppresses_and_penalizes(monkeypatch):
    monkeypatch.setattr(quality.http, "get",
                        lambda *a, **k: FakeResp(200, _posting("Requires 5 years of experience building services.")))
    monkeypatch.setattr(quality.llm, "complete", lambda *a, **k:
                        '{"years_required": 5, "new_grad": "no", "role_family": "swe", "reason": "wants 5 years"}')
    monkeypatch.setattr(quality, "is_marquee", lambda c: False)
    rec = _rec()
    assert quality.verify(rec)
    assert rec["alert_ok"] is False
    assert rec["score"] == 80 - quality._PENALTY_NOT_NEW_GRAD
    assert any("not new-grad" in r for r in rec["score_reasons"])
    assert rec["quality"]["new_grad"] == "no"


def test_marquee_keeps_alert_but_records_verdict(monkeypatch):
    monkeypatch.setattr(quality.http, "get",
                        lambda *a, **k: FakeResp(200, _posting("Minimum 4 years experience.")))
    monkeypatch.setattr(quality.llm, "complete", lambda *a, **k:
                        '{"years_required": 4, "new_grad": "no", "role_family": "swe", "reason": "4 yrs"}')
    monkeypatch.setattr(quality, "is_marquee", lambda c: True)
    rec = _rec(company="Anthropic")
    assert quality.verify(rec)
    assert rec["alert_ok"] is True          # the Shams rule outranks the LLM
    assert rec["quality"]["new_grad"] == "no"
    assert rec["score"] < 80                # penalty still informs ranking


def test_non_technical_role_demoted(monkeypatch):
    monkeypatch.setattr(quality.http, "get",
                        lambda *a, **k: FakeResp(200, _posting("You will manage the sales pipeline.")))
    monkeypatch.setattr(quality.llm, "complete", lambda *a, **k:
                        '{"years_required": null, "new_grad": "yes", "role_family": "non-technical", "reason": "sales role"}')
    monkeypatch.setattr(quality, "is_marquee", lambda c: False)
    rec = _rec(title="Solutions Engineer")
    assert quality.verify(rec)
    assert rec["alert_ok"] is False
    assert rec["score"] == 80 - quality._PENALTY_WRONG_ROLE


def test_reapply_is_idempotent_without_rescore(monkeypatch):
    monkeypatch.setattr(quality, "is_marquee", lambda c: False)
    rec = _rec(quality={"checked_at": NOW, "live": True, "new_grad": "no",
                        "years_required": 3, "role_family": "swe", "reason": "x"})
    quality.reapply(rec)
    once = rec["score"]
    quality.reapply(rec)
    quality.reapply(rec)
    assert rec["score"] == once             # no compounding
    assert sum("not new-grad" in r for r in rec["score_reasons"]) == 1


def test_reapply_restores_after_rescore(monkeypatch):
    monkeypatch.setattr(quality, "is_marquee", lambda c: False)
    rec = _rec(quality={"checked_at": NOW, "live": True, "new_grad": "no",
                        "years_required": 3, "role_family": "swe", "reason": "x"})
    quality.reapply(rec)
    # simulate enrich re-score: score() rebuilds score + reasons from scratch
    rec["score"] = 80
    rec["score_reasons"] = ["base 40", "role:swe +20"]
    rec["alert_ok"] = True
    quality.reapply(rec)
    assert rec["score"] == 80 - quality._PENALTY_NOT_NEW_GRAD
    assert rec["alert_ok"] is False


def test_unreadable_page_retries_then_marks_unclear(monkeypatch):
    monkeypatch.setattr(quality.http, "get", lambda *a, **k: FakeResp(200, "<html>tiny</html>"))
    rec = _rec()
    assert quality.verify(rec) is False     # attempt 1: retry later
    assert "checked_at" not in rec["quality"]
    assert quality.verify(rec) is True      # attempt 2: give up as unclear
    assert rec["quality"]["new_grad"] == "unclear"
    assert rec["alert_ok"] is True          # unclear never suppresses


def test_garbage_llm_output_never_crashes(monkeypatch):
    monkeypatch.setattr(quality.http, "get",
                        lambda *a, **k: FakeResp(200, _posting("A fine job posting with plenty of text.")))
    monkeypatch.setattr(quality.llm, "complete", lambda *a, **k: "not json at all")
    rec = _rec()
    assert quality.verify(rec) is False
    assert quality.verify(rec) is True      # capped at _MAX_ATTEMPTS
    assert rec["quality"]["new_grad"] == "unclear"


def test_one_to_two_years_demotes_but_never_hides(monkeypatch):
    # the house rule is 3+ yrs = out; an LLM "no" at 1-2 yrs only re-ranks
    monkeypatch.setattr(quality.http, "get",
                        lambda *a, **k: FakeResp(200, _posting("1-2 years of experience preferred.")))
    monkeypatch.setattr(quality.llm, "complete", lambda *a, **k:
                        '{"years_required": 1, "new_grad": "no", "role_family": "swe", "reason": "1+ yrs"}')
    monkeypatch.setattr(quality, "is_marquee", lambda c: False)
    rec = _rec()
    assert quality.verify(rec)
    assert rec["alert_ok"] is True
    assert rec["score"] == 80 - quality._PENALTY_SOME_EXPERIENCE


def test_limit_budgets_attempts_not_successes(monkeypatch):
    # unreadable pages must consume the budget — no unbounded fetch sprees
    monkeypatch.setattr(quality.http, "get", lambda *a, **k: FakeResp(200, "<html>tiny</html>"))
    monkeypatch.setattr(quality.time, "sleep", lambda s: None)
    jobs = {f"j{i}": _rec(id=f"j{i}", url=f"https://x.example/{i}") for i in range(10)}
    reapplied, verified = quality.run(jobs, limit=4)
    assert verified == 0
    touched = sum(1 for r in jobs.values() if r.get("quality", {}).get("attempts"))
    assert touched == 4


def test_run_respects_limit_and_reapplies(monkeypatch):
    monkeypatch.setattr(quality.http, "get", lambda *a, **k: FakeResp(404))
    monkeypatch.setattr(quality, "is_marquee", lambda c: False)
    monkeypatch.setattr(quality.time, "sleep", lambda s: None)
    jobs = {f"j{i}": _rec(id=f"j{i}", url=f"https://x.example/{i}") for i in range(5)}
    jobs["old"] = _rec(id="old", first_seen=NOW - 90 * 86400)
    verified_cap = 2
    reapplied, verified = quality.run(jobs, limit=verified_cap)
    assert verified == verified_cap
    assert reapplied == 0                   # nothing had a stored verdict yet
    reapplied2, verified2 = quality.run(jobs, limit=0)
    assert reapplied2 == verified_cap       # stored verdicts re-applied
