import time

from radar import posting
from radar.models import Job

NOW = int(time.time())
PAD = " The team ships production systems and values ownership." * 6


def test_analyze_sponsorship_no_variants():
    for phrase in ["We are unable to sponsor visas at this time.",
                   "This role does not offer visa sponsorship.",
                   "Applicants must be a US citizen for this position.",
                   "No sponsorship is available for this role.",
                   "Candidates must be authorized to work without sponsorship."]:
        a = posting.analyze(phrase + PAD)
        assert a["sponsorship"] == "no", phrase
        assert a["sponsorship_note"]


def test_analyze_sponsorship_yes_and_unknown():
    a = posting.analyze("Visa sponsorship is available for exceptional candidates." + PAD)
    assert a["sponsorship"] == "yes"
    a = posting.analyze("H-1B sponsorship offered. Join our infra team." + PAD)
    assert a["sponsorship"] == "yes"
    a = posting.analyze("We build robots. Great benefits. Free lunch." + PAD)
    assert a["sponsorship"] == "unknown"


def test_analyze_years_shapes():
    cases = [
        ("Requires 5+ years of professional experience.", 5),
        ("Minimum of 3 years experience with Python.", 3),
        ("At least three years of backend experience.", 3),
        ("You have 0-2 years of experience shipping software.", 0),
        ("1 to 3 years of relevant experience.", 1),
        ("7 years' experience in distributed systems.", 7),
    ]
    for text, want in cases:
        a = posting.analyze(text + PAD)
        assert a.get("years_min") == want, text
        assert a.get("years_note"), text
    a = posting.analyze("New grads welcome, no prior experience needed." + PAD)
    assert "years_min" not in a


def test_analyze_intern_counts_and_short_text():
    a = posting.analyze("2+ years of experience, including internships and co-ops." + PAD)
    assert a.get("intern_counts") is True
    assert posting.analyze("too short") == {}


def test_apply_demotes_three_plus_years_and_is_idempotent():
    rec = {"alert_ok": True, "score_reasons": []}
    posting.apply_record(rec, {"sponsorship": "unknown", "years_min": 4,
                               "years_note": "4+ years"}, fetched=True, now=NOW)
    assert rec["alert_ok"] is False
    assert rec["score_reasons"] == ["posting: wants 4+ yrs (dashboard only)"]
    posting.reapply(rec)
    posting.reapply(rec)
    assert rec["score_reasons"].count("posting: wants 4+ yrs (dashboard only)") == 1


def test_sponsorship_demotes_only_when_needed(monkeypatch):
    rec = {"alert_ok": True, "score_reasons": []}
    a = {"sponsorship": "no", "sponsorship_note": "unable to sponsor"}
    monkeypatch.setattr(posting, "needs_sponsorship", lambda: False)
    posting.apply_record(rec, a, fetched=True, now=NOW)
    assert rec["alert_ok"] is True          # informational by default
    monkeypatch.setattr(posting, "needs_sponsorship", lambda: True)
    posting.reapply(rec)
    assert rec["alert_ok"] is False
    assert "posting: no visa sponsorship (dashboard only)" in rec["score_reasons"]


def _job(**over):
    kw = dict(company="Acme", title="Software Engineer, New Grad",
              url="https://boards.example.com/j/1", source="greenhouse",
              locations=["NYC"], alert_ok=True, score=80)
    kw.update(over)
    return Job(**kw)


def test_scrape_pass_inline_fetch_and_stored(monkeypatch):
    fetched_urls = []
    def fake_fetch(url):
        fetched_urls.append(url)
        if url.endswith("/dead"):
            return False, ""
        return True, "Minimum of 3 years experience required. No sponsorship." + PAD
    monkeypatch.setattr("radar.quality.fetch_posting", fake_fetch)
    monkeypatch.setattr(posting.time, "sleep", lambda s: None)

    inline = _job(description="We welcome new grads. 0-2 years experience. Sponsorship available." + PAD)
    fetch_me = _job(url="https://boards.example.com/j/2", title="ML Engineer, New Grad")
    dead = _job(url="https://boards.example.com/j/dead", title="Data Engineer", score=70)
    stored = {"s1": {"id": "s1", "company": "Held Co", "title": "Software Engineer",
                     "url": "https://boards.example.com/j/3", "source": "greenhouse",
                     "alert_ok": True, "score": 75, "score_reasons": [],
                     "first_seen": NOW - 86400}}
    stats = posting.scrape_pass([inline, fetch_me, dead], stored, {}, NOW, budget=10)

    assert stats["inline"] == 1
    assert inline.posting["sponsorship"] == "yes" and inline.posting["years_min"] == 0
    assert inline.alert_ok is True

    assert fetch_me.posting["years_min"] == 3
    assert fetch_me.alert_ok is False        # 3+ yrs demoted, not deleted

    assert dead.alert_ok is False
    assert "posting gone (link checked)" in dead.score_reasons

    assert stored["s1"]["posting"]["years_min"] == 3
    assert stored["s1"]["alert_ok"] is False
    assert stats["fetched"] == 3 and stats["closed"] == 1 and stats["demoted"] == 2


def test_scrape_pass_respects_budget(monkeypatch):
    calls = {"n": 0}
    def fake_fetch(url):
        calls["n"] += 1
        return True, "text too short"
    monkeypatch.setattr("radar.quality.fetch_posting", fake_fetch)
    monkeypatch.setattr(posting.time, "sleep", lambda s: None)
    jobs = [_job(url=f"https://x.example/{i}") for i in range(8)]
    stats = posting.scrape_pass(jobs, {}, {}, NOW, budget=3)
    assert calls["n"] == 3 and stats["fetched"] == 3


def test_scrape_pass_revisits_pre_research_priority_job_once(monkeypatch):
    fetched = []
    saved = {}
    monkeypatch.setattr("radar.quality.fetch_posting", lambda url: (
        fetched.append(url) or True,
        "Acme is a technology company. We build safe industrial systems for customers. " + PAD,
    ))
    monkeypatch.setattr("radar.company_research.load", lambda: {})
    monkeypatch.setattr("radar.company_research.save", lambda records: saved.update(records))
    monkeypatch.setattr("radar.state.load", lambda name, default: default)
    monkeypatch.setattr(posting.time, "sleep", lambda s: None)
    stored = {"old": {
        "id": "old", "company": "Acme", "title": "Software Engineer, New Grad",
        "url": "https://boards.example.com/j/old", "source": "greenhouse",
        "alert_ok": True, "score": 82, "score_reasons": [],
        "first_seen": NOW - 86400,
        "posting": {"analyzed_at": NOW - 3600, "sponsorship": "unknown"},
    }}

    stats = posting.scrape_pass([], stored, {}, NOW, budget=2)

    assert fetched == ["https://boards.example.com/j/old"]
    assert stats["research_sources"] == 1
    assert stored["old"]["research_checked_at"] == NOW
    assert "acme" in saved

    # A failed/empty evidence extraction is retried weekly, not every crawl.
    fetched.clear()
    posting.scrape_pass([], stored, {}, NOW + 3600, budget=2)
    assert fetched == []


def test_summary_tags():
    assert posting.summary_tags(None) == ""
    assert posting.summary_tags({"sponsorship": "no", "years_min": 2}) == \
        "🛂 no sponsorship · ⏳ 2+ yrs"
    assert posting.summary_tags({"sponsorship": "unknown", "intern_counts": True}) == \
        "⏳ internships count"
