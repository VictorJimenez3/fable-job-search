import time

from radar.models import Job
from radar.score import gates, score, update_feedback_from_applied

NOW = int(time.time())
FB = {"company_boosts": {}, "token_boosts": {}, "negative_companies": []}


def mk(title, company="Acme", locations=None, source="simplify", desc="", **kw):
    return Job(company=company, title=title, url="https://x.com/j", source=source,
               locations=locations or ["New York, NY"], description=desc, **kw)


def test_gates_reject_senior_and_intern():
    assert gates(mk("Senior Software Engineer"))[0] is False
    assert gates(mk("Staff ML Engineer"))[0] is False
    assert gates(mk("Software Engineering Intern"))[0] is False
    assert gates(mk("Software Engineer III"))[0] is False


def test_gates_reject_non_us():
    keep, _, _ = gates(mk("Software Engineer, New Grad", locations=["Bangalore, India"]))
    assert keep is False
    keep, _, _ = gates(mk("Software Engineer, New Grad", locations=["Toronto, Canada"]))
    assert keep is False


def test_gates_accept_new_grad_us():
    keep, alert_ok, _ = gates(mk("Software Engineer, New Grad", locations=["Remote"]))
    assert keep and alert_ok


def test_gates_direct_ats_needs_entry_signal():
    # direct ATS posting without any new-grad language: kept, but not alertable
    keep, alert_ok, _ = gates(mk("Software Engineer", source="greenhouse"))
    assert keep is True and alert_ok is False
    # ...unless the description signals entry level
    keep, alert_ok, _ = gates(mk("Software Engineer", source="greenhouse",
                                 desc="We welcome new grad applicants with 0-2 years experience."))
    assert keep and alert_ok


def test_gates_marquee_company_alerts_without_entry_signal():
    # The Shams rule: an Anthropic (or any marquee) posting from a direct ATS
    # alerts even with zero new-grad wording...
    keep, alert_ok, reasons = gates(mk("Research Engineer, Interpretability",
                                       company="Anthropic", source="greenhouse"))
    assert keep and alert_ok
    assert "marquee company (auto-alert)" in reasons
    # ...but the hard gates still silence marquee senior/intern roles
    assert gates(mk("Senior Research Engineer", company="Anthropic", source="greenhouse"))[0] is False
    assert gates(mk("Research Intern", company="OpenAI", source="greenhouse"))[0] is False


def test_gates_pays_bank_alerts_without_entry_signal():
    keep, alert_ok, reasons = gates(mk("Software Engineer", source="greenhouse",
                                       salary="$160,000 - $210,000"))
    assert keep and alert_ok
    assert "pays bank (auto-alert)" in reasons
    # below the bar: dashboard only, as before
    keep, alert_ok, _ = gates(mk("Software Engineer", source="greenhouse",
                                 salary="$95,000 - $120,000"))
    assert keep is True and alert_ok is False


def test_pays_bank_parses_salary_shapes():
    from radar.score import pays_bank
    assert pays_bank("$150k+")
    assert pays_bank("up to 175K")
    assert pays_bank("$140,000 - $185,000")
    assert not pays_bank("$60/hr")
    assert not pays_bank("$120k")
    assert not pays_bank("")


def test_gates_reject_years_requirement():
    keep, _, _ = gates(mk("Software Engineer", source="greenhouse",
                          desc="Requires 5+ years of production experience."))
    assert keep is False


def test_score_prefers_healthtech_ai_over_generic_swe():
    ai_health = mk("Machine Learning Engineer, New Grad", company="Tempus")
    ai_health.sector = "healthtech"
    ai_health.posted_at = NOW - 3600
    score(ai_health, FB, NOW)

    generic = mk("Software Engineer, New Grad", company="RandomCo")
    generic.sector = "other"
    generic.posted_at = NOW - 6 * 86400
    score(generic, FB, NOW)

    assert ai_health.score > generic.score + 15
    assert any("healthtech" in r for r in ai_health.score_reasons)


def test_feedback_boosts_applied_companies():
    fb = {"company_boosts": {}, "token_boosts": {}, "negative_companies": []}
    update_feedback_from_applied(fb, "Commure", "Machine Learning Engineer")
    j = mk("Machine Learning Engineer, New Grad", company="Commure")
    j.sector = "healthtech"
    score(j, fb, NOW)
    assert any("engaged with" in r for r in j.score_reasons)


def test_negative_feedback_penalizes():
    fb = {"company_boosts": {}, "token_boosts": {}, "negative_companies": ["acme"]}
    j = mk("Software Engineer, New Grad")
    j2 = mk("Software Engineer, New Grad")
    score(j, fb, NOW)
    score(j2, FB, NOW)
    assert j.score == j2.score - 10
