import pytest

from radar.internship_radar import _internship_evidence_pass

NOW = 1_900_000_000


def _stored(key, *, checked_at=None, score=77):
    evidence = {"internship_signal": "source_only"}
    if checked_at is not None:
        evidence["fetch_checked_at"] = checked_at
    return {
        "id": key,
        "company": "Acme",
        "title": "Software Engineering Intern",
        "url": f"https://jobs.example/{key}",
        "source": "zero2sudo_notion",
        "link_resolution": {"status": "source_only", "source_note": "curated"},
        "internship_eligibility": evidence,
        "posting_status": "open",
        "score": score,
        "score_reasons": ["owner reason"],
        "alert_ok": False,
        "first_seen": NOW - 100,
        "lifecycle_events": [{"status": "open", "ts": NOW - 10}],
        "owner_status": "applied",
    }


def test_failed_stored_fetch_preserves_score_reasons_owner_and_lifecycle(monkeypatch):
    row = _stored("failed")
    monkeypatch.setattr("radar.quality.fetch_posting", lambda url: (None, ""))
    stats = _internship_evidence_pass([], {"failed": row}, NOW, budget=1)
    assert stats == {"selected": 1, "fetched": 1, "confirmed": 0, "errors": 1}
    assert row["score"] == 77
    assert row["score_reasons"][0] == "owner reason"
    assert row["owner_status"] == "applied"
    assert row["lifecycle_events"]
    assert row["internship_fetch_checked_at"] == NOW
    assert row["link_resolution"]["status"] == "source_only"


def test_failed_fetch_cooldown_survives_second_rescore(monkeypatch):
    row = _stored("twice")
    calls = []
    monkeypatch.setattr("radar.quality.fetch_posting", lambda url: (calls.append(url) or (None, "")))
    _internship_evidence_pass([], {"twice": row}, NOW, budget=1)
    second = _internship_evidence_pass([], {"twice": row}, NOW + 60, budget=1)
    assert calls == ["https://jobs.example/twice"]
    assert second["selected"] == 0


def test_dead_stored_fetch_closes_record_without_rewriting_score(monkeypatch):
    row = _stored("dead")
    monkeypatch.setattr("radar.quality.fetch_posting", lambda url: (False, ""))
    _internship_evidence_pass([], {"dead": row}, NOW, budget=1)
    assert row["posting_status"] == "expired"
    assert row["alert_ok"] is False
    assert row["score"] == 77
    assert row["score_reasons"][0] == "owner reason"


def test_generic_landing_page_does_not_resolve_source_only_lead(monkeypatch):
    row = _stored("acme/careers/software-engineering-intern")
    landing = ("Responsibilities: learn about software opportunities. Qualifications: "
               "our internships are for students. Apply now to browse openings. " * 10)
    monkeypatch.setattr("radar.quality.fetch_posting", lambda url: (True, landing))
    _internship_evidence_pass([], {"landing": row}, NOW, budget=1)
    assert row["link_resolution"]["status"] == "source_only"
    assert row["internship_fetch_status"] == "unreadable"
    assert row["score"] == 77


def test_full_time_jd_with_curator_intern_title_stays_source_only(monkeypatch):
    row = _stored("acme/careers/software-engineering-intern")
    text = ("Responsibilities: build software engineering systems. "
            "Qualifications: full-time permanent role for experienced engineers. " * 8)
    monkeypatch.setattr("radar.quality.fetch_posting", lambda url: (True, text))
    _internship_evidence_pass([], {"ft": row}, NOW, budget=1)
    assert row["link_resolution"]["status"] == "source_only"
    assert row["internship_fetch_status"] == "unreadable"


def test_off_role_job_description_does_not_resolve_internship_lead(monkeypatch):
    row = _stored("acme/careers/electrician")
    text = ("Responsibilities: wire commercial panels as an electrician. "
            "Qualifications: current apprentices may apply; safety certification required. " * 6)
    monkeypatch.setattr("radar.quality.fetch_posting", lambda url: (True, text))
    _internship_evidence_pass([], {"offrole": row}, NOW, budget=1)
    assert row["link_resolution"]["status"] == "source_only"
    assert row["internship_fetch_status"] == "unreadable"


def test_budget_caps_fetches_and_untried_lead_precedes_retry(monkeypatch):
    rows = {"old": _stored("old", checked_at=NOW - 4 * 86400),
            "new": _stored("new")}
    calls = []
    monkeypatch.setattr("radar.quality.fetch_posting", lambda url: (calls.append(url) or (None, "")))
    stats = _internship_evidence_pass([], rows, NOW, budget=1)
    assert stats["selected"] == stats["fetched"] == 1
    assert calls == ["https://jobs.example/new"]


def test_annotated_new_job_keeps_fetch_attempt_metadata(monkeypatch):
    from radar.internship import annotate
    from radar.models import Job

    job = Job(company="Acme", title="Software Engineering Intern",
              url="https://jobs.example/new", source="zero2sudo_notion",
              internship_eligibility={"fetch_checked_at": NOW,
                                      "fetch_status": "unreadable"})
    annotate(job)
    assert job.internship_eligibility["fetch_checked_at"] == NOW
    assert job.internship_eligibility["fetch_status"] == "unreadable"


def test_confirmed_fetched_jd_keeps_posting_quality_hints(monkeypatch):
    row = _stored("acme/careers/hints")
    text = ("Responsibilities: build software as an intern. "
            "Qualifications: current students may apply; 2 years of experience preferred. " * 5)
    monkeypatch.setattr("radar.quality.fetch_posting", lambda url: (True, text))
    _internship_evidence_pass([], {"hints": row}, NOW, budget=1)
    assert row["link_resolution"]["status"] == "resolved"
    assert row["posting"]["fetched"] is True
    assert row["posting"].get("years_preferred_min") == 2


def test_confirmed_fetch_does_not_reopen_manually_archived_row(monkeypatch):
    row = _stored("acme/careers/archived")
    row["manual_archived"] = True
    monkeypatch.setattr("radar.quality.fetch_posting", lambda url: pytest.fail("archived rows must not spend the fetch budget"))
    _internship_evidence_pass([], {"archived": row}, NOW, budget=1)
    assert row["link_resolution"]["status"] == "source_only"
    assert row["alert_ok"] is False
    assert row["manual_archived"] is True


def test_new_unknown_full_time_role_is_not_spent_in_internship_fetch_budget(monkeypatch):
    from radar.models import Job

    job = Job(company="Acme", title="Software Engineer", url="https://jobs.example/acme/role-123",
              source="greenhouse", source_board="ats:greenhouse")
    monkeypatch.setattr("radar.quality.fetch_posting",
                        lambda url: pytest.fail("unknown full-time role must not be fetched"))
    stats = _internship_evidence_pass([job], {}, NOW, budget=1)
    assert stats["selected"] == 0


def test_different_technical_internship_cannot_confirm_curator_title(monkeypatch):
    row = _stored("acme/careers/data-science-intern")
    row["title"] = "Data Science Intern"
    text = ("Responsibilities: as an intern you will build software interfaces. "
            "Qualifications: undergraduate students may apply for this software internship. " * 5)
    monkeypatch.setattr("radar.quality.fetch_posting", lambda url: (True, text))
    _internship_evidence_pass([], {"different": row}, NOW, budget=1)
    assert row["link_resolution"]["status"] == "source_only"


def test_employer_fetch_budget_has_a_hard_ceiling(monkeypatch):
    rows = {str(i): _stored(str(i)) for i in range(81)}
    monkeypatch.setattr("radar.quality.fetch_posting", lambda url: (None, ""))
    stats = _internship_evidence_pass([], rows, NOW, budget=10000)
    assert stats["selected"] == stats["fetched"] == 80
