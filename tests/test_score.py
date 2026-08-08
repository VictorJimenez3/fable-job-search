import time

from radar.models import Job
from radar import main
from radar.score import (RULES_VERSION, apply_company_concentration,
                         calibrate_score, early_career_possible, gates, role_bucket,
                         regate, score, update_feedback_from_applied)
from radar.sector import infer

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


def test_current_role_order_puts_general_swe_above_data_engineering():
    swe = mk("Software Engineer, New Grad")
    data = mk("Data Engineer, New Grad")
    score(swe, FB, NOW)
    score(data, FB, NOW)
    assert swe.score > data.score
    assert any("role:swe" in r for r in swe.score_reasons)
    assert any("role:data_eng" in r for r in data.score_reasons)


def test_gates_direct_ats_needs_entry_signal():
    # direct ATS posting without any new-grad language: kept, but not alertable
    keep, alert_ok, _ = gates(mk("Software Engineer", source="greenhouse"))
    assert keep is True and alert_ok is False
    # ...unless the description signals entry level
    keep, alert_ok, _ = gates(mk("Software Engineer", source="greenhouse",
                                 desc="We welcome new grad applicants with 0-2 years experience."))
    assert keep and alert_ok


def test_early_career_possible_is_visible_but_never_new_grad_evidence():
    fanatics = mk("AI Engineer", company="Fanatics", source="greenhouse")
    keep, alert_ok, _ = gates(fanatics)
    assert keep and not alert_ok
    assert early_career_possible(fanatics, {}) is True
    assert early_career_possible(fanatics, {"years_min": 1}) is False
    assert early_career_possible(mk("Senior AI Engineer", source="greenhouse"), {}) is False
    assert early_career_possible(mk("AI Engineer", source="simplify"), {}) is False


def test_trusted_new_grad_board_source_supplies_missing_title_signal():
    job = mk("Software Engineer", source="simplify")
    keep, alert_ok, reasons = gates(job)
    assert keep and alert_ok
    assert any("verified new-grad" in r for r in reasons)


def test_preferred_sports_and_video_game_companies_are_classified():
    assert infer("PlayStation", {}) == "video_games"
    assert infer("Fanatics", {}) == "sports"


def test_gates_marquee_company_still_requires_new_grad_signal():
    # Prestige is competitive context, not permission to bypass new-grad fit.
    keep, alert_ok, reasons = gates(mk("Research Engineer, Interpretability",
                                       company="Anthropic", source="greenhouse"))
    assert keep and not alert_ok
    assert any("not verified new-grad" in r for r in reasons)
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
    # on-field marquee titles still need new-grad evidence
    keep, alert_ok, _ = gates(mk("Research Engineer, Interpretability",
                                 company="Anthropic", source="greenhouse"))
    assert keep and not alert_ok


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


def test_pm_family_is_visible_low_scoring_and_never_alertable():
    titles = [
        "Product Manager, New Grad",
        "Technical Product Manager, New Grad",
        "Product Owner, New Grad",
        "Project Manager, New Grad",
        "Business Analyst, New Grad",
        "UX/UI Researcher, New Grad",
        "Solutions Architect, New Grad",
        "Associate - Digital Product Management, New Grad",
    ]
    for title in titles:
        job = mk(title, source="simplify")
        keep, alert_ok, reasons = gates(job)
        assert keep and not alert_ok, title
        assert role_bucket(title) == "pm", title
        assert any("PM-family role" in reason for reason in reasons), title
        score(job, FB, NOW)
        assert any("role:pm +0" in reason for reason in job.score_reasons), title


def test_standalone_pm_does_not_capture_maintenance_or_shift_titles():
    for title in ["PM Technician", "PM Shift", "PM Administrative Assistant"]:
        assert role_bucket(title) != "pm", title


def test_google_new_grad_favorite_is_100_but_pm_stays_low():
    technical = mk("Software Engineer, New Grad", company="Google")
    score(technical, FB, NOW)
    assert technical.score == 100
    assert any("Google new-grad -> 100" in reason for reason in technical.score_reasons)

    pm = mk("Product Manager, New Grad", company="Google", source="simplify")
    score(pm, FB, NOW)
    assert pm.score < 100
    assert any("role:pm +0" in reason for reason in pm.score_reasons)


def test_google_override_survives_company_concentration():
    jobs = [
        mk("Deep Learning Engineer, New Grad", company="Google"),
        mk("Machine Learning Engineer, New Grad", company="Google"),
        mk("Software Engineer, New Grad", company="Google"),
    ]
    for job in jobs:
        score(job, FB, NOW)
    apply_company_concentration(jobs)
    assert all(job.score == 100 for job in jobs)
    assert all(job.ranking_adjustment == 0 for job in jobs)


def test_gates_ai_customer_roles_are_dashboard_only():
    for title in ["AI Success Engineer", "AI Governance and Advisory Associate"]:
        keep, alert_ok, reasons = gates(mk(title, company="OpenAI"))
        assert keep is True and alert_ok is False, title
        assert any("off-field title" in r for r in reasons), title


def test_gates_priority_sector_still_requires_new_grad_signal():
    # Healthtech + a technical title is valuable, but still cannot override
    # the new-grad gate.
    keep, alert_ok, reasons = gates(mk("Software Engineer", company="Eight Sleep",
                                       source="greenhouse", sector="healthtech"))
    assert keep and not alert_ok
    assert any("not verified new-grad" in r for r in reasons)
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
    assert keep and not alert_ok
    assert any("not verified new-grad" in r for r in reasons)
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


def test_gates_rejects_any_positive_required_experience_floor():
    keep, _, reasons = gates(mk("Software Engineer, New Grad", source="greenhouse",
                                desc="Requires 1+ years of professional experience."))
    assert keep is False
    assert "not new-grad" in reasons[0]


def test_gates_keeps_zero_to_two_year_new_grad_role():
    keep, alert_ok, _ = gates(mk("Software Engineer", source="greenhouse",
                                 desc="New grads welcome; 0-2 years of experience."))
    assert keep and alert_ok


def test_gates_alerts_technical_leadership_programs():
    keep, alert_ok, reasons = gates(mk(
        "Technology Leadership Development Program",
        company="Johnson & Johnson", source="greenhouse",
        desc="Two-year program across software engineering, data science, and digital health."))
    assert keep and alert_ok

    program = mk("Data Science Leadership Development Program - Associate Data Scientist",
                  company="Travelers", source="greenhouse")
    score(program, FB, NOW)
    assert program.score >= 66
    assert any("technical leadership program" in r for r in program.score_reasons)
    assert any("technical leadership" in r for r in reasons)

    keep, alert_ok, _ = gates(mk(
        "Emerging Talent Rotational Program - IT", company="Merck",
        source="greenhouse", desc="AI/ML, data, and software rotations."))
    assert keep and alert_ok


def test_gates_rejects_off_field_leadership_programs():
    keep, alert_ok, reasons = gates(mk(
        "Finance Leadership Development Program",
        company="Merck", source="greenhouse",
        desc="A rotational program developing future finance leaders."))
    assert keep and not alert_ok
    assert any("off-field" in r for r in reasons)


def test_score_prefers_healthtech_ai_over_generic_swe():
    ai_health = mk("Machine Learning Engineer, New Grad", company="Tempus")
    ai_health.sector = "healthtech"
    ai_health.posted_at = NOW - 3600
    score(ai_health, FB, NOW)

    generic = mk("Software Engineer, New Grad", company="RandomCo")
    generic.sector = "other"
    generic.posted_at = NOW - 6 * 86400
    score(generic, FB, NOW)

    assert ai_health.score >= generic.score + 15
    assert any("healthtech" in r for r in ai_health.score_reasons)


def test_v8_calibration_is_monotonic_and_reserves_top_end():
    raw_values = [0, 35, 55, 70, 85, 100, 115, 125, 150]
    scores = [calibrate_score(value) for value in raw_values]
    assert scores == sorted(scores)
    assert calibrate_score(55) == 66
    assert calibrate_score(100) == 94
    assert calibrate_score(125) == 100


def test_v8_role_wording_separates_same_goal_company(monkeypatch):
    from radar import score as score_module
    monkeypatch.setattr(score_module, "_COMPANY_RESEARCH_CACHE", {
        "nvidia": {
            "ai_ds_prestige_tier": {"confidence": "high", "source_ids": ["a"], "value": "Top-tier Tier 1 industry leader"},
            "pace_of_work": {"confidence": "high", "source_ids": ["b"], "value": "Fast-paced rapid iteration"},
            "technical_work": {"confidence": "high", "source_ids": ["c"], "value": "AI/ML infrastructure and distributed training"},
            "size_stage": {"confidence": "high", "source_ids": ["d"], "value": "Global public company"},
        }
    })
    fb = {"company_boosts": {"nvidia": 8}, "token_boosts": {}, "negative_companies": []}
    frontier = mk("Deep Learning Engineer, New Grad", company="NVIDIA", salary="$210k-$260k")
    hardware = mk("Digital Logic Tools Engineer, New Grad", company="NVIDIA", salary="$172k")
    frontier.sector = hardware.sector = "big_tech"
    score(frontier, fb, NOW)
    score(hardware, fb, NOW)
    assert frontier.score == 100
    assert 90 <= hardware.score < frontier.score
    assert frontier.score_raw > hardware.score_raw
    assert frontier.score_dimensions["compensation"] > hardware.score_dimensions["compensation"]


def test_v8_superpower_can_beat_sector_without_named_exception():
    health = mk("Software Engineer, New Grad", company="Small Health Co", sector="healthtech")
    high_pay = mk("Machine Learning Engineer, New Grad", company="Unlisted Systems", sector="other",
                  salary="$225,000-$275,000")
    score(health, FB, NOW)
    score(high_pay, FB, NOW)
    assert health.score < 90
    assert high_pay.score > health.score
    assert any("compensation ceiling" in reason for reason in high_pay.score_reasons)


def test_v8_persists_auditable_dimensions_and_raw_utility():
    job = mk("Machine Learning Platform Engineer, New Grad", salary="$180k")
    score(job, FB, NOW)
    assert job.score_raw == sum(job.score_dimensions.values())
    assert set(job.score_dimensions) == {
        "base", "role_fit", "eligibility", "mission", "company_quality",
        "compensation", "personal_signal", "timing_access",
    }
    assert any(f"calibration v{RULES_VERSION}" in reason for reason in job.score_reasons)


def test_new_grad_priority_beats_prestigious_experienced_role():
    eligible = mk("Software Engineer, New Grad", company="RandomCo")
    experienced = mk("Senior Machine Learning Engineer", company="NVIDIA", source="greenhouse")
    # The senior title is normally gated out; model the dashboard-only case
    # directly to assert the ranking policy itself.
    experienced.title = "Machine Learning Engineer"
    score(eligible, FB, NOW)
    score(experienced, FB, NOW)
    assert eligible.score > experienced.score
    assert any("new-grad/early-career priority" in r for r in eligible.score_reasons)
    assert any("early-career possible" in r for r in experienced.score_reasons)


def test_company_concentration_protects_best_and_nudges_weaker_roles():
    jobs = [
        mk("Deep Learning Engineer, New Grad", company="NVIDIA"),
        mk("Machine Learning Engineer, New Grad", company="NVIDIA"),
        mk("Software Engineer, New Grad", company="NVIDIA"),
        mk("Software Engineer, New Grad", company="OtherCo"),
    ]
    for job in jobs:
        score(job, FB, NOW)
    before = [job.score for job in jobs[:3]]
    apply_company_concentration(jobs)
    assert jobs[0].ranking_adjustment == 0
    assert jobs[1].ranking_adjustment <= 0
    assert jobs[2].ranking_adjustment <= jobs[1].ranking_adjustment
    assert jobs[0].score == before[0]
    assert any("company concentration" in reason for reason in jobs[2].score_reasons)


def test_company_concentration_does_not_touch_small_groups_or_raw_ties():
    two = [mk("Software Engineer, New Grad", company="NVIDIA") for _ in range(2)]
    for job in two:
        score(job, FB, NOW)
    apply_company_concentration(two)
    assert all(job.ranking_adjustment == 0 for job in two)

    tied = [mk("Software Engineer, New Grad", company="NVIDIA") for _ in range(3)]
    for job in tied:
        score(job, FB, NOW)
    apply_company_concentration(tied)
    assert all(job.ranking_adjustment == 0 for job in tied)


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
    assert flipped == 1  # only a changes; d remains suppressed by its verdict


def test_regate_requires_new_grad_for_priority_sector_jobs():
    jobs = {"w": _stored("Software Engineer, New Grad", company="WHOOP",
                         sector="healthtech", alert_ok=False)}
    assert regate(jobs) == 1
    assert jobs["w"]["alert_ok"] is True
    assert jobs["w"]["explicit_new_grad"] is True


def test_score_health_requires_current_version_and_reasons(tmp_path, monkeypatch, capsys):
    from radar import state
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    state.save("jobs.json", {
        "ok": {"score_version": RULES_VERSION, "rules_v": RULES_VERSION,
               "score_reasons": []},
        "old": {"score_version": RULES_VERSION - 1, "rules_v": RULES_VERSION,
                "score_reasons": []},
    })
    assert main.score_health_cmd() == 1
    assert "1 record(s)" in capsys.readouterr().out
