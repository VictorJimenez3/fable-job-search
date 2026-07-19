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
    rec = _rec()
    assert quality.verify(rec)
    assert rec["alert_ok"] is False
    assert rec["score"] == 80 - quality._PENALTY_NOT_NEW_GRAD
    assert any("not new-grad" in r for r in rec["score_reasons"])
    assert rec["quality"]["new_grad"] == "no"


def test_marquee_now_suppressed_by_verdict(monkeypatch):
    # DECISIONS #31: the Shams rule no longer outranks a verified-seniority
    # verdict — a marquee posting that wants 4 yrs is demoted like anyone else
    monkeypatch.setattr(quality.http, "get",
                        lambda *a, **k: FakeResp(200, _posting("Minimum 4 years experience.")))
    monkeypatch.setattr(quality.llm, "complete", lambda *a, **k:
                        '{"years_required": 4, "new_grad": "no", "role_family": "swe", "reason": "4 yrs"}')
    rec = _rec(company="Anthropic")
    assert quality.verify(rec)
    assert rec["alert_ok"] is False
    assert rec["quality"]["new_grad"] == "no"
    assert rec["score"] == 80 - quality._PENALTY_NOT_NEW_GRAD


def test_non_technical_role_demoted(monkeypatch):
    monkeypatch.setattr(quality.http, "get",
                        lambda *a, **k: FakeResp(200, _posting("You will manage the sales pipeline.")))
    monkeypatch.setattr(quality.llm, "complete", lambda *a, **k:
                        '{"years_required": null, "new_grad": "yes", "role_family": "non-technical", "reason": "sales role"}')
    rec = _rec(title="Solutions Engineer")
    assert quality.verify(rec)
    assert rec["alert_ok"] is False
    assert rec["score"] == 80 - quality._PENALTY_WRONG_ROLE


def test_reapply_is_idempotent_without_rescore(monkeypatch):
    rec = _rec(quality={"checked_at": NOW, "live": True, "new_grad": "no",
                        "years_required": 3, "role_family": "swe", "reason": "x"})
    quality.reapply(rec)
    once = rec["score"]
    quality.reapply(rec)
    quality.reapply(rec)
    assert rec["score"] == once             # no compounding
    assert sum("not new-grad" in r for r in rec["score_reasons"]) == 1


def test_reapply_restores_after_rescore(monkeypatch):
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


def test_one_to_two_years_suppresses_alert(monkeypatch):
    # New-grad-first policy treats any positive required experience floor as
    # dashboard-only, including an LLM-confirmed 1-2 year requirement.
    monkeypatch.setattr(quality.http, "get",
                        lambda *a, **k: FakeResp(200, _posting("1-2 years of experience preferred.")))
    monkeypatch.setattr(quality.llm, "complete", lambda *a, **k:
                        '{"years_required": 1, "new_grad": "no", "role_family": "swe", "reason": "1+ yrs"}')
    rec = _rec()
    assert quality.verify(rec)
    assert rec["alert_ok"] is False
    assert rec["score"] == 80 - quality._PENALTY_NOT_NEW_GRAD


def test_verify_pasted_grades_and_is_idempotent(monkeypatch):
    calls = {"n": 0}
    def fake_llm(*a, **k):
        calls["n"] += 1
        return '{"years_required": null, "new_grad": "yes", "role_family": "swe", "reason": "entry role"}'
    monkeypatch.setattr(quality.llm, "complete", fake_llm)
    rec = _rec()
    jd = "We are hiring a software engineer to join our platform team. " * 5
    assert quality.verify_pasted(rec, jd) is True
    assert rec["quality"]["source"] == "pasted"
    assert rec["quality"]["new_grad"] == "yes"
    assert rec["quality"]["jd_sha"]
    # same paste again: no new LLM call
    assert quality.verify_pasted(rec, jd) is False
    assert calls["n"] == 1
    # an edited paste is re-judged
    assert quality.verify_pasted(rec, jd + " Requires 6 years.") is True
    assert calls["n"] == 2


def test_verify_pasted_rejects_garbage_and_short_text(monkeypatch):
    monkeypatch.setattr(quality.llm, "complete", lambda *a, **k: "not json")
    rec = _rec()
    assert quality.verify_pasted(rec, "too short") is False
    assert quality.verify_pasted(rec, "long enough text " * 20) is False
    assert "checked_at" not in rec.get("quality", {})


def test_verify_pasted_suppresses_senior_posting(monkeypatch):
    monkeypatch.setattr(quality.llm, "complete", lambda *a, **k:
                        '{"years_required": 5, "new_grad": "no", "role_family": "swe", "reason": "5 yrs"}')
    rec = _rec(company="Anthropic")   # marquee no longer shields (DECISIONS #31)
    assert quality.verify_pasted(rec, "Requires five years of experience. " * 10)
    assert rec["alert_ok"] is False


class SpaResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


def test_spa_workday_fetches_cxs_json(monkeypatch):
    seen = {}
    def get(url, **kw):
        seen["url"] = url
        return SpaResp(200, {"jobPostingInfo": {"jobDescription":
            "<p>Entry level role. 0-2 years experience.</p>"}})
    monkeypatch.setattr(quality.http, "get", get)
    rec = _rec(url="https://nvidia.wd5.myworkdayjobs.com/en-US/nvidiaexternalcareersite"
                   "/job/US-CA-Santa-Clara/Software-Engineer_JR123")
    alive, text = quality.fetch_posting_spa(rec)
    assert seen["url"] == ("https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/"
                           "nvidiaexternalcareersite/job/US-CA-Santa-Clara/Software-Engineer_JR123")
    assert alive is True and "Entry level role" in text
    # locale-less URLs (vansh links) parse too
    rec2 = _rec(url="https://usbank.wd1.myworkdayjobs.com/US_Bank_Careers/job/Earth-City-MO/SWE_2026")
    quality.fetch_posting_spa(rec2)
    assert seen["url"] == ("https://usbank.wd1.myworkdayjobs.com/wday/cxs/usbank/"
                           "US_Bank_Careers/job/Earth-City-MO/SWE_2026")
    # 404 = posting gone
    monkeypatch.setattr(quality.http, "get", lambda *a, **k: SpaResp(404))
    assert quality.fetch_posting_spa(rec) == (False, "")


def test_spa_oracle_fetches_requisition_details(monkeypatch):
    seen = {}
    def get(url, **kw):
        seen["url"] = url
        return SpaResp(200, {"items": [{"ExternalDescriptionStr": "<b>Analyst program</b>",
                                        "ExternalQualificationsStr": "BS in CS"}]})
    monkeypatch.setattr(quality.http, "get", get)
    rec = _rec(url="https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210743637")
    alive, text = quality.fetch_posting_spa(rec)
    assert "recruitingCEJobRequisitionDetails" in seen["url"]
    assert 'siteNumber=CX_1001,Id=%22210743637%22' in seen["url"]
    assert alive is True and "Analyst program" in text and "BS in CS" in text
    # an empty items list means the requisition is no longer served
    monkeypatch.setattr(quality.http, "get", lambda *a, **k: SpaResp(200, {"items": []}))
    assert quality.fetch_posting_spa(rec) == (False, "")


def test_spa_eightfold_uses_domain_map(monkeypatch):
    seen = {}
    def get(url, **kw):
        seen["url"] = url
        return SpaResp(200, {"job_description": "<p>Build streaming infra.</p>"})
    monkeypatch.setattr(quality.http, "get", get)
    rec = _rec(company="Netflix", source="eightfold",
               url="https://explore.jobs.netflix.net/careers/job/790317054101")
    alive, text = quality.fetch_posting_spa(rec, {"netflix": "netflix.com"})
    assert seen["url"] == ("https://explore.jobs.netflix.net/api/apply/v2/jobs/"
                           "790317054101?domain=netflix.com")
    assert alive is True and "streaming infra" in text


def test_verify_routes_spa_urls_through_json_api(monkeypatch):
    monkeypatch.setattr(quality, "fetch_posting_spa",
                        lambda rec, domains=None: (True, "New grad role, 0-1 years. " * 20))
    monkeypatch.setattr(quality, "fetch_posting",
                        lambda url: (_ for _ in ()).throw(AssertionError("plain fetch used for SPA url")))
    monkeypatch.setattr(quality.llm, "complete", lambda *a, **k:
                        '{"years_required": 0, "new_grad": "yes", "role_family": "swe", "reason": "entry"}')
    rec = _rec(url="https://x.wd5.myworkdayjobs.com/en-US/site/job/loc/Role_1")
    assert quality.verify(rec)
    assert rec["quality"]["new_grad"] == "yes"


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
    monkeypatch.setattr(quality.time, "sleep", lambda s: None)
    jobs = {f"j{i}": _rec(id=f"j{i}", url=f"https://x.example/{i}") for i in range(5)}
    jobs["old"] = _rec(id="old", first_seen=NOW - 90 * 86400)
    verified_cap = 2
    reapplied, verified = quality.run(jobs, limit=verified_cap)
    assert verified == verified_cap
    assert reapplied == 0                   # nothing had a stored verdict yet
    reapplied2, verified2 = quality.run(jobs, limit=0)
    assert reapplied2 == verified_cap       # stored verdicts re-applied
