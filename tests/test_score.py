import time

from radar.models import Job
from radar.score import (RULES_VERSION, gates, regate, score,
                         update_feedback_from_applied)

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


def test_gates_reject_numeric_levels():
    # rules v2: numeric levels are as disqualifying as roman ones
    assert gates(mk("Software Engineer 3"))[0] is False
    assert gates(mk("Software Engineer L5"))[0] is False
    assert gates(mk("Machine Learning Engineer, Level 4"))[0] is False


def test_gates_demote_midlevel_but_keep_visible():
    # "II"/"L4"-class titles are typically 1-3 yrs: dashboard yes, alert no
    for title in ["Software Engineer II", "Software Engineer, L4", "Mid-Level Backend Engineer"]:
        keep, alert_ok, reasons = gates(mk(title, company="Anthropic", source="greenhouse"))
        assert keep is True and alert_ok is False, title
        assert any("mid-level title" in r for r in reasons), title


def test_gates_off_field_beats_marquee():
    # DECISIONS #31: field fit outranks the Shams rule — a marquee Safeguards/
    # policy/sales title never alerts, but stays on the dashboard
    for title, company in [("Research Engineer, Safeguards", "Anthropic"),
                           ("Trust & Safety Operations Analyst", "OpenAI"),
                           ("Solutions Engineer, Machine Learning", "Google")]:
        keep, alert_ok, reasons = gates(mk(title, company=company, source="greenhouse"))
        assert keep is True and alert_ok is False, title
        assert any("off-field title" in r for r in reasons), title
    # on-field marquee titles still auto-alert
    keep, alert_ok, _ = gates(mk("Research Engineer, Interpretability",
                                 company="Anthropic", source="greenhouse"))
    assert keep and alert_ok


def test_gates_off_field_beats_explicit_new_grad():
    keep, alert_ok, reasons = gates(mk("New Grad Software Engineer, Customer Success"))
    assert keep is True and alert_ok is False
    assert any("off-field title" in r for r in reasons)


def test_gates_off_field_false_positive_guards():
    # narrow by construction: technical titles containing risky words survive
    keep, alert_ok, _ = gates(mk("Security Engineer, New Grad"))
    assert keep and alert_ok
    keep, _, reasons = gates(mk("Embedded Software Engineer, New Grad"))
    assert keep
    assert not any("off-field" in r for r in reasons)


def test_gates_role_fit_is_title_led_not_description_led():
    # Company/JD prose is saturated with AI/software terms. It cannot promote
    # a clearly unrelated title into the technical-role funnel.
    for title in ["Safety Transparency Editor", "Research Associate, Biology",
                  "Shipping & Receiving Materials Associate", "Associate, Actuarial"]:
        keep, alert_ok, reasons = gates(mk(
            title, company="OpenAI", source="greenhouse",
            desc="We build artificial intelligence software with machine learning engineers."))
        assert keep is False and alert_ok is False, title
        assert "not an AI/SWE/DS role" in reasons

    # A technical title can still use description text as entry-level proof.
    keep, alert_ok, _ = gates(mk(
        "Software Engineer", source="greenhouse",
        desc="This entry-level role welcomes recent graduates."))
    assert keep and alert_ok


def test_gates_generic_analyst_titles_do_not_count_as_data_science():
    for title in ["Provider Configuration Analyst", "Care Strategy Analyst",
                  "Regulatory Operations Analyst"]:
        keep, alert_ok, reasons = gates(mk(title))
        assert keep is True and alert_ok is False, title
        assert any("dashboard only" in r for r in reasons), title
    for title in ["Data Analyst", "Product Analyst", "Quantitative Analyst",
                  "Analytics Engineer"]:
        assert gates(mk(title))[0] is True, title


def test_gates_ai_customer_roles_are_dashboard_only():
    for title in ["AI Success Engineer", "AI Governance and Advisory Associate"]:
        keep, alert_ok, reasons = gates(mk(title, company="OpenAI"))
        assert keep is True and alert_ok is False, title
        assert any("off-field title" in r for r in reasons), title


def test_gates_priority_sector_alerts_on_strong_title():
    # the WHOOP lesson: healthtech + a real engineering title alerts without
    # new-grad wording (ATS postings carry no description to prove it).
    # Non-marquee company on purpose: WHOOP itself now alerts via marquee.
    keep, alert_ok, reasons = gates(mk("Software Engineer", company="Eight Sleep",
                                       source="greenhouse", sector="healthtech"))
    assert keep and alert_ok
    assert any("priority sector" in r for r in reasons)
    # same title outside a priority sector: dashboard only
    keep, alert_ok, _ = gates(mk("Software Engineer", company="Stripe",
                                 source="greenhouse", sector="fintech"))
    assert keep is True and alert_ok is False
    # a bare "<anything> Analyst" title is too weak even in a priority sector
    keep, alert_ok, _ = gates(mk("Patient Relations Analyst", company="CVS Health",
                                 source="greenhouse", sector="healthtech"))
    assert keep is True and alert_ok is False


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


def test_feedback_stopwords_never_learned_and_inert():
    # learning: off-field/generic tokens are never written...
    fb = {"company_boosts": {}, "token_boosts": {}, "negative_companies": []}
    update_feedback_from_applied(fb, "Acme", "Full-Time Product Solutions Analyst")
    assert "product" not in fb["token_boosts"]
    assert "solutions" not in fb["token_boosts"]
    assert "full" not in fb["token_boosts"]
    assert "analyst" in fb["token_boosts"]   # field-relevant tokens still learn
    # ...and stale entries already in feedback.json stop scoring
    stale = {"company_boosts": {}, "negative_companies": [],
             "token_boosts": {"business": 4, "marketing": 3}}
    j = mk("Business Marketing Engineer, New Grad")
    j2 = mk("Business Marketing Engineer, New Grad")
    score(j, stale, NOW)
    score(j2, FB, NOW)
    assert j.score == j2.score


def _stored(title, company="Anthropic", **over):
    rec = {"id": title, "company": company, "title": title, "url": "https://x.com/j",
           "source": "greenhouse", "locations": ["New York, NY"], "salary": "",
           "remote": False, "sector": "", "score": 80,
           "score_reasons": ["base 40"], "alert_ok": True}
    rec.update(over)
    return rec


def test_regate_applies_current_rules_to_stored_jobs():
    jobs = {
        # stale marquee off-field alert from rules v1 → demoted
        "a": _stored("Research Engineer, Safeguards"),
        # closed job → untouched entirely
        "b": _stored("Software Engineer", closed_at=NOW, rules_v=1),
        # already re-gated → untouched
        "c": _stored("Trust & Safety Analyst", rules_v=RULES_VERSION),
        # quality-suppressed job stays suppressed even if gates would promote
        "d": _stored("Machine Learning Engineer", company="WHOOP",
                     sector="healthtech", alert_ok=False,
                     quality={"checked_at": NOW, "live": True, "new_grad": "no",
                              "years_required": 5, "role_family": "ml", "reason": "x"}),
    }
    flipped = regate(jobs)
    assert jobs["a"]["alert_ok"] is False
    assert any(f"re-gate v{RULES_VERSION}" in r for r in jobs["a"]["score_reasons"])
    assert jobs["a"]["rules_v"] == RULES_VERSION
    assert jobs["b"]["alert_ok"] is True and jobs["b"]["rules_v"] == 1  # never re-opened/touched
    assert jobs["c"]["alert_ok"] is True                                 # version stamp respected
    assert jobs["d"]["alert_ok"] is False                                # verdict re-applied last
    assert flipped == 2  # a demoted; d flipped up by gates then re-suppressed


def test_regate_promotes_priority_sector_jobs():
    jobs = {"w": _stored("Software Engineer", company="WHOOP",
                         sector="healthtech", alert_ok=False)}
    assert regate(jobs) == 1
    assert jobs["w"]["alert_ok"] is True
    assert jobs["w"]["explicit_new_grad"] is False
