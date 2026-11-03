import time
from unittest.mock import patch

import pytest

from radar import state
from radar.internship import analyze, annotate, gates, match, score
from radar.models import Job
from radar.sources import aggregators, ats


def job(title="Software Engineering Intern", *, company="Acme", source="greenhouse", description="",
        locations=None, **kwargs):
    return Job(company=company, title=title, url="https://example.test/job/1",
               source=source, locations=locations or ["New York, NY"],
               description=description, **kwargs)


def test_graduation_window_is_explicit_and_matches_viewer_date():
    posting = job(
        description="Students graduating between May 2027 and May 2029 may apply."
    )
    eligibility = analyze(posting)
    assert eligibility["status"] == "explicit"
    assert eligibility["graduation_start"] == "2027-05-01"
    assert eligibility["graduation_end"] == "2029-05-01"
    assert match(eligibility, "2028-05") == "match"
    assert match(eligibility, "2030-05") == "mismatch"


def test_class_of_year_is_not_lost_when_it_matches_the_start_year():
    posting = job(
        title="Summer 2027 Software Engineering Intern",
        description="Applicants should be in the class of 2027."
    )
    eligibility = analyze(posting)
    assert eligibility["graduation_start"] == "2027-01-01"
    assert match(eligibility, "2027-05") == "match"


def test_class_years_are_derived_from_internship_start_and_graduation_date():
    posting = job(
        title="Summer 2027 Software Engineering Intern",
        description="Open to rising juniors and seniors."
    )
    eligibility = analyze(posting)
    assert eligibility["term_start"] == "2027-06-01"
    assert set(eligibility["class_years"]) == {"junior", "senior"}
    # A May 2028 graduate is a rising senior for Summer 2027; a May 2029
    # graduate is a rising junior.
    assert match(eligibility, "2028-05") == "match"
    assert match(eligibility, "2029-05") == "match"
    assert match(eligibility, "2030-05") == "mismatch"


def test_first_year_synonym_maps_to_freshman():
    eligibility = analyze(job(description="Open to first year students."))
    assert eligibility["class_years"] == ["freshman"]


def test_unknown_or_open_eligibility_never_hides_a_role():
    posting = job(title="Data Science Intern")
    eligibility = analyze(posting)
    assert eligibility["status"] == "open"
    assert match(eligibility, "2029-05") == "open"
    assert match(eligibility, None) == "unknown"


def test_internship_evidence_is_position_specific_and_term_aware():
    ordinary = job(
        title="Software Engineer",
        description=("Full-time permanent role. Work closely with senior engineers "
                     "on our production systems."),
    )
    annotate(ordinary)
    assert ordinary.internship_eligibility["internship_signal"] == "unknown"
    assert gates(ordinary)[0:2] == (True, False)

    unrelated = job(
        title="Medium Voltage Electrician",
        description="Our AI company works with senior technicians on campus.",
    )
    annotate(unrelated)
    assert gates(unrelated)[0:2] == (False, False)

    past = job(title="Software Engineering Intern — Summer 2026",
               description="Current undergraduate students welcome.")
    annotate(past)
    assert gates(past)[0:2] == (True, False)
    assert any("past internship term" in reason for reason in gates(past)[2])

    prior_experience = job(
        title="Software Engineer",
        description="We require 2 years of experience, including internship experience.",
    )
    annotate(prior_experience)
    assert gates(prior_experience)[1] is False

    company_program = job(
        title="Software Engineer",
        description="Our internship program welcomes students. This permanent position needs 4 years experience.",
    )
    annotate(company_program)
    assert gates(company_program)[1] is False

    service_audience = job(
        title="Software Engineer",
        description="We help current undergraduate students apply to colleges.",
    )
    annotate(service_audience)
    assert gates(service_audience)[1] is False

    recruiting = job(
        title="Early Careers & Interns Specialist",
        description="Looking for a dedicated Early Careers & Interns Specialist to help us build a recruiting program.",
    )
    annotate(recruiting)
    assert recruiting.internship_eligibility["internship_signal"] == "unknown"
    assert gates(recruiting)[0:2] == (False, False)


def test_internship_degree_requirement_distinguishes_required_phd_from_preference():
    required = job(
        title="Software Engineering Intern — Summer 2027",
        description="Must be enrolled in a PhD program in computer science.",
    )
    annotate(required)
    assert required.internship_eligibility["degree_requirement"] == "graduate_only"
    assert gates(required)[0:2] == (True, False)

    preferred = job(
        title="Software Engineering Intern — Summer 2027",
        description="PhD preferred; current bachelor's students may apply.",
    )
    annotate(preferred)
    assert preferred.internship_eligibility["degree_requirement"] == "undergraduate_compatible"
    assert gates(preferred)[0:2] == (True, True)

    graduate_only = job(
        title="Software Engineering Intern — Summer 2027",
        description="Graduate students only.",
    )
    annotate(graduate_only)
    assert graduate_only.internship_eligibility["degree_requirement"] == "graduate_only"
    assert gates(graduate_only)[0:2] == (True, False)


def test_term_year_is_not_reused_as_graduation_year():
    posting = job(
        title="Software Engineering Intern — Summer 2027",
        description="Applicants must be in the class of 2025.",
    )
    eligibility = analyze(posting)
    assert eligibility["term_start"] == "2027-06-01"
    assert eligibility["graduation_start"] == "2025-01-01"
    assert eligibility["graduation_end"] == "2025-12-01"
    assert match(eligibility, "2027-05") == "mismatch"


def test_internship_term_options_accept_reversed_and_shared_season_copy():
    shared = analyze(job(title="Spring/Summer 2027 Software Engineering Intern"))
    assert shared["term_options"] == ["2027-01-01", "2027-06-01"]
    reversed_order = analyze(job(title="2027 Summer Software Engineering Intern"))
    assert reversed_order["term_start"] == "2027-06-01"


def test_generic_intern_title_uses_technical_duty_fallback():
    technical = job(title="Research Intern", description="Build machine learning models for ranking.")
    annotate(technical)
    assert gates(technical)[0:2] == (True, True)

    off_field = job(
        title="Research Intern",
        description="About us: xAI builds artificial intelligence systems. Work with senior technicians.",
    )
    annotate(off_field)
    assert gates(off_field)[0:2] == (False, False)
    assert any("target technical" in reason for reason in gates(off_field)[2])


def test_mixed_terms_match_any_term_for_cohort():
    eligibility = analyze(job(
        title="Spring/Summer 2027 Software Engineering Intern",
        description="Open to rising juniors.",
    ))
    assert match(eligibility, "2028-05") == "match"
    assert match(eligibility, "2027-05") == "mismatch"


def test_legacy_body_signal_is_not_resurrected_without_posting_text():
    rehydrated = job(title="Software Engineer")
    rehydrated.internship_eligibility = {
        "evidence_version": 1,
        "status": "open",
        "internship_signal": "body",
        "employment_signal": "internship_or_student",
        "evidence": ["Our internship program exists."],
    }
    annotate(rehydrated)
    assert rehydrated.internship_eligibility["internship_signal"] == "unknown"
    assert gates(rehydrated)[1] is False


def test_v2_proof_snippets_replay_body_evidence_without_retaining_the_jd():
    original = job(
        title="Software Engineer",
        description="Current undergraduate students may apply. Build production services.",
    )
    annotate(original)
    rehydrated = job(title=original.title)
    rehydrated.internship_eligibility = dict(original.internship_eligibility)
    annotate(rehydrated)
    assert rehydrated.internship_eligibility["evidence_version"] == 2
    assert rehydrated.internship_eligibility["internship_signal"] == "body"
    assert gates(rehydrated)[1] is True


def test_v1_class_year_without_proof_is_not_trusted_on_rescore():
    rehydrated = job(title="Software Engineer")
    rehydrated.internship_eligibility = {
        "evidence_version": 1,
        "status": "explicit",
        "class_years": ["senior"],
        "internship_signal": "body",
        "evidence": ["Work with senior engineers."],
    }
    annotate(rehydrated)
    assert rehydrated.internship_eligibility["class_years"] == []
    assert rehydrated.internship_eligibility["internship_signal"] == "unknown"


def test_v1_proof_migrates_once_and_is_idempotent():
    rehydrated = job(title="Software Engineer")
    rehydrated.internship_eligibility = {
        "evidence_version": 1,
        "status": "open",
        "internship_signal": "body",
        "evidence": ["Current undergraduate students may apply."],
    }
    annotate(rehydrated)
    first = dict(rehydrated.internship_eligibility)
    annotate(rehydrated)
    second = dict(rehydrated.internship_eligibility)
    assert first == second
    assert second["evidence_version"] == 2
    assert second["internship_signal"] == "body"


def test_enrichment_fetch_metadata_survives_annotation():
    posting = job(title="Software Engineering Intern")
    posting.internship_eligibility = {
        "fetch_checked_at": 1_900_000_000,
        "fetch_status": "readable",
        "fetch_note": "employer page confirmed",
    }
    annotate(posting)
    assert posting.internship_eligibility["fetch_checked_at"] == 1_900_000_000
    assert posting.internship_eligibility["fetch_status"] == "readable"
    assert posting.internship_eligibility["fetch_note"] == "employer page confirmed"


def test_fresh_employer_requirements_replace_old_source_proof_on_rescore():
    from radar.internship_radar import _job_from_record

    posting = job(description="Requirements: Graduate students only. Must be enrolled in a PhD program.")
    posting.internship_eligibility = {
        "source_signal": True, "evidence_version": 2,
        "evidence": ["Undergraduate students may apply.", "Open to rising juniors.",
                     "Bachelor students are welcome.", "Students graduating in 2028 may apply."],
    }
    annotate(posting)
    assert posting.internship_eligibility["degree_requirement"] == "graduate_only"
    rehydrated = _job_from_record(posting.to_record())
    annotate(rehydrated)
    assert rehydrated.internship_eligibility["degree_requirement"] == "graduate_only"
    assert not gates(rehydrated)[1]


def test_hydrated_roundtrip_fixtures_keep_source_only_and_graduate_only_stable():
    from radar.internship_radar import _job_from_record

    fixtures = [
        {
            "id": "665dd4bdb10f2ce8", "title": "Junior .NET Developer - Fresh Graduates",
            "source": "workday", "url": "https://jobs.example.test/1",
            "internship_eligibility": {
                "source_signal": True, "internship_signal": "source_only",
                "status": "open", "evidence": ["Workday internship search: student"],
            },
        },
        {
            "id": "a49b183205542c61", "title": "Junior Java Developer (part-time student)",
            "source": "workday", "url": "https://jobs.example.test/2",
            "internship_eligibility": {
                "source_signal": True, "internship_signal": "source_only",
                "status": "open", "evidence": ["Workday internship search: student"],
            },
        },
        {
            "id": "d5f466bab5ae1acc", "title": "Quantitative Researcher",
            "source": "greenhouse", "url": "https://jobs.example.test/3",
            "internship_eligibility": {
                "internship_signal": "body", "status": "explicit",
                "class_years": ["senior"],
                "evidence": [
                    "Experience Required: Entry-level (PhD Program) or Experienced (Postdoc, Faculty, Scientific Lab) Education: PhD in Math, Science, Engineering"
                ],
            },
        },
    ]
    for fixture in fixtures:
        posting = _job_from_record(fixture)
        annotate(posting)
        first = dict(posting.internship_eligibility)
        annotate(posting)
        second = dict(posting.internship_eligibility)
        assert first == second
        if fixture["id"] != "d5f466bab5ae1acc":
            assert second["internship_signal"] == "source_only"
        else:
            assert second["degree_requirement"] == "graduate_only"
            assert second["internship_signal"] == "unknown"


def test_internship_gates_keep_target_roles_but_separate_alert_evidence():
    explicit = job(description="Current undergraduate students graduating in 2028.")
    annotate(explicit)
    keep, alert_ok, reasons = gates(explicit)
    assert keep and alert_ok
    assert any("graduation/class-year" in reason for reason in reasons)

    terse_ats = job(title="Software Engineer", source="greenhouse")
    annotate(terse_ats)
    assert gates(terse_ats)[0:2] == (True, False)

    curated = job(title="Software Engineer", source="simplify_internship")
    annotate(curated)
    assert gates(curated)[0:2] == (True, False)

    for title in ("Software Engineer Intern", "Summer 2027 Software Engineer", "Software Engineer Co-op"):
        posting = job(title=title, source="greenhouse")
        annotate(posting)
        assert gates(posting)[0:2] == (True, True)

    experienced = job(title="Sr. Software Engineer Intern")
    annotate(experienced)
    assert experienced.internship_eligibility["internship_signal"] == "experienced_title"
    assert gates(experienced)[0:2] == (False, False)

    ordinary_experienced = job(title="Senior Software Engineer")
    annotate(ordinary_experienced)
    assert ordinary_experienced.internship_eligibility["internship_signal"] == "experienced_title"

    rehydrated_experienced = job(title="Senior Software Engineer")
    rehydrated_experienced.internship_eligibility = {"internship_signal": "unknown"}
    annotate(rehydrated_experienced)
    assert rehydrated_experienced.internship_eligibility["internship_signal"] == "experienced_title"

    assert gates(job(title="Senior Software Engineering Intern"))[0] is False
    assert gates(job(locations=["London, United Kingdom"]))[0] is False


def test_malformed_internship_url_stays_visible_but_never_alertable():
    malformed = job(title="Software Engineering Intern")
    malformed.url = "https:/.workable.com/trexquant/j/A634E0E3F4"
    annotate(malformed)
    keep, alert_ok, reasons = gates(malformed)
    assert keep is True and alert_ok is False
    assert "invalid posting URL (dashboard/review only)" in reasons

    valid = job(title="Software Engineering Intern")
    valid.url = "https://jobs/123"
    annotate(valid)
    assert gates(valid)[0:2] == (True, True)

    experienced = job(title="Senior Software Engineering Intern")
    experienced.url = malformed.url
    annotate(experienced)
    assert gates(experienced)[0:2] == (False, False)


def test_source_only_internship_lead_stays_review_only_even_with_intern_title():
    posting = job(
        title="Software Engineering Intern",
        source="zero2sudo_notion",
        link_resolution={"status": "source_only", "source_note": "curator note"},
    )
    annotate(posting)
    keep, alert_ok, reasons = gates(posting)
    assert keep and not alert_ok
    assert posting.internship_eligibility["internship_signal"] == "source_only"
    assert any("review" in reason for reason in reasons)


def test_rehydrated_source_only_internship_preserves_resolution():
    from radar.internship_radar import _job_from_record

    posting = _job_from_record({
        "company": "Acme",
        "title": "Software Engineering Intern",
        "url": "https://example.test/job/1",
        "source": "zero2sudo_notion",
        "profile": "internship",
        "link_resolution": {"status": "source_only"},
        "internship_eligibility": {"internship_signal": "source_only"},
    })
    annotate(posting)
    assert posting.link_resolution["status"] == "source_only"
    assert posting.internship_eligibility["internship_signal"] == "source_only"
    assert gates(posting)[1] is False


def test_internship_posting_identity_collapses_provider_application_suffix():
    from radar.main import _existing_posting_ids, _state_job_identity

    existing = {
        "stable": {
            "url": "https://jobs.lever.co/acme/role-123",
            "company": "Acme",
            "title": "Software Engineering Intern",
        }
    }
    variant = Job(
        company="Acme",
        title="Software Engineering Intern",
        url="https://jobs.lever.co/acme/role-123/apply",
        locations=["New York, NY"],
        source="zero2sudo_notion",
        link_resolution={"status": "source_only"},
    )
    assert _state_job_identity(variant, existing, _existing_posting_ids(existing)) == "stable"


def test_official_internship_sighting_wins_over_zero2sudo_variant():
    from radar.internship_radar import _dedupe_internship_sightings
    from radar.main import _merge_job_sighting

    curated = Job(
        company="Acme", title="Software Engineering Intern",
        url="https://jobs.lever.co/acme/role-123/apply", locations=["New York, NY"],
        source="zero2sudo_notion",
        link_resolution={"status": "source_only", "source_note": "curator"},
    )
    official = Job(
        company="Acme", title="Software Engineering Intern",
        url="https://jobs.lever.co/acme/role-123", locations=["New York, NY"],
        source="lever", ats="lever", description="Current undergraduate students may apply.",
        link_resolution={"status": "resolved", "resolved_url": "https://jobs.lever.co/acme/role-123"},
    )
    for ordered in ([curated, official], [official, curated]):
        rows, dropped = _dedupe_internship_sightings(ordered, _merge_job_sighting)
        assert dropped == 1
        assert rows[0].url == official.url
        assert rows[0].description == official.description
        assert rows[0].link_resolution["status"] == "resolved"
        assert rows[0].link_resolution["source_only_provenance"]


def test_explicit_full_time_outliers_are_review_only_but_not_deleted(monkeypatch):
    monkeypatch.setenv("RADAR_PROFILE", "internship")
    from radar import config
    monkeypatch.setattr(config, "_profile_cache", {})
    posting = job(
        title="Software Engineer, Full-Time",
        company="Google",
        description="Build distributed systems for our production platform.",
    )
    annotate(posting)
    keep, alert_ok, reasons = gates(posting)
    assert keep and not alert_ok
    assert posting.internship_eligibility["employment_signal"] == "full_time_only"
    assert any("full-time-only" in reason for reason in reasons)
    score(posting, int(time.time()))
    assert posting.score == 34
    assert posting.score_raw > posting.score
    assert any("full-time review cap applied" in reason for reason in posting.score_reasons)


def test_full_time_return_offer_does_not_demote_a_real_internship():
    posting = job(
        title="Software Engineering Intern",
        description="Current students may apply; strong interns may receive a full-time offer.",
    )
    annotate(posting)
    keep, alert_ok, _ = gates(posting)
    assert keep and alert_ok
    assert posting.internship_eligibility["employment_signal"] == "internship_or_student"


def test_internship_score_has_auditable_reasons(monkeypatch):
    monkeypatch.setenv("RADAR_PROFILE", "internship")
    from radar import config
    monkeypatch.setattr(config, "_profile_cache", {})
    posting = job(description="Current undergraduate students graduating in 2028.", remote=True)
    annotate(posting)
    score(posting, int(time.time()))
    assert posting.score > 0
    assert any("flat across role families" in reason for reason in posting.score_reasons)
    assert any("graduation eligibility" in reason for reason in posting.score_reasons)
    assert not any("remote" in reason for reason in posting.score_reasons)
    assert "personal_signal" not in posting.score_dimensions


def test_internship_score_has_small_healthcare_preference_and_ignores_role_remote(monkeypatch):
    monkeypatch.setenv("RADAR_PROFILE", "internship")
    from radar import config
    monkeypatch.setattr(config, "_profile_cache", {})
    details = "Current undergraduate students graduating in 2028. Work with mentors to ship customer-facing features."
    ai = job(title="Machine Learning Intern", description=details, remote=False)
    swe = job(title="Software Engineering Intern", description=details, remote=True)
    ai.sector = "healthtech"
    swe.sector = "other"
    annotate(ai)
    annotate(swe)
    score(ai, int(time.time()))
    score(swe, int(time.time()))
    assert ai.score == swe.score + 4
    assert ai.score_dimensions["sector_preference"] == 4
    assert swe.score_dimensions["sector_preference"] == 0
    assert ai.score_dimensions["role_fit"] == swe.score_dimensions["role_fit"]
    assert any(reason == "healthcare preference +4" for reason in ai.score_reasons)
    assert not any("remote" in reason.lower() for reason in ai.score_reasons)


def test_internship_score_rewards_pay_prestige_and_work_without_personal_signals(monkeypatch):
    monkeypatch.setenv("RADAR_PROFILE", "internship")
    from radar import config
    monkeypatch.setattr(config, "_profile_cache", {})
    now = int(time.time())
    strong = job(
        title="Software Engineering Intern", company="NVIDIA",
        salary="$45/hour", description=(
            "Current undergraduate students graduating in 2028. "
            "Mentorship and professional development. Own and deploy production systems at scale. "
            "Return offer possible."
        ), posted_at=now,
    )
    ordinary = job(
        title="Data Science Intern", company="Acme Labs", salary="$18/hour",
        description="Current undergraduate students graduating in 2028.", posted_at=now - 8 * 86400,
    )
    annotate(strong)
    annotate(ordinary)
    score(strong, now)
    score(ordinary, now)
    assert strong.score > ordinary.score
    assert strong.score_dimensions["compensation"] > ordinary.score_dimensions["compensation"]
    assert strong.score_dimensions["prestige"] > ordinary.score_dimensions["prestige"]
    assert strong.score_dimensions["work_quality"] > ordinary.score_dimensions["work_quality"]
    assert any("compensation ceiling" in reason for reason in strong.score_reasons)
    assert any("prestige tier" in reason and "crackedness" in reason for reason in strong.score_reasons)
    assert any("hands-on ownership" in reason for reason in strong.score_reasons)
    assert not any(any(token in reason.lower() for token in
                       ("saved/applied", "feedback", "victor", "your "))
                   for reason in strong.score_reasons)


def test_internship_score_uses_full_opportunity_scale(monkeypatch):
    monkeypatch.setenv("RADAR_PROFILE", "internship")
    from radar import config
    monkeypatch.setattr(config, "_profile_cache", {})
    now = int(time.time())
    google = job(
        company="Google", salary="$60/hour", posted_at=now - 20 * 86400,
        description="Current undergraduate students may apply.",
    )
    exceptional = job(
        company="NVIDIA", salary="$65/hour", posted_at=now,
        description=(
            "Applicants graduating in 2028. Mentorship and professional development. "
            "Own and deploy production systems at scale. Research architecture and performance. "
            "Return offer possible."
        ),
    )
    for posting in (google, exceptional):
        annotate(posting)
        score(posting, now)
    assert google.score >= 80
    assert google.score_dimensions["prestige"] == 65
    assert google.score_dimensions["compensation"] == 8
    assert exceptional.score > google.score
    assert exceptional.score <= 100


def test_internship_prestige_is_separate_from_personal_company_preferences(monkeypatch):
    monkeypatch.setenv("RADAR_PROFILE", "internship")
    from radar import config
    monkeypatch.setattr(config, "_profile_cache", {})
    now = int(time.time())
    google = job(company="Google", salary="$30/hour", description="Students may apply.")
    unknown = job(company="Acme Labs", salary="$30/hour", description="Students may apply.")
    for posting in (google, unknown):
        annotate(posting)
        score(posting, now)
    assert google.score_dimensions["prestige"] == 65
    assert unknown.score_dimensions["prestige"] == 0
    assert google.score > unknown.score
    assert not any(token in " ".join(google.score_reasons).lower()
                   for token in ("victor", "saved/applied", "personal_signal"))


def test_internship_prestige_and_defense_policy(monkeypatch):
    monkeypatch.setenv("RADAR_PROFILE", "internship")
    from radar import config
    monkeypatch.setattr(config, "_profile_cache", {})
    now = int(time.time())
    pinterest = job(company="Pinterest", description="Current undergraduates may apply.")
    healthcare = job(company="Eli Lilly", description="Current undergraduates may apply.")
    bank = job(company="Citigroup", description="Current undergraduates may apply.")
    defense = job(company="Anduril", description="Current undergraduates may apply.")
    for posting in (pinterest, healthcare, bank, defense):
        annotate(posting)
        score(posting, now)
    assert pinterest.score_dimensions["prestige"] == 65
    assert healthcare.score_dimensions["sector_preference"] == 4
    assert bank.score_dimensions["sector_preference"] == -4
    assert defense.score <= 12
    assert defense.score_dimensions["defense_penalty"] == -70
    assert any("automatic low-priority penalty -70" in reason for reason in defense.score_reasons)


def test_internship_annotation_preserves_cohort_and_work_evidence_on_rescore(monkeypatch):
    monkeypatch.setenv("RADAR_PROFILE", "internship")
    from radar import config
    monkeypatch.setattr(config, "_profile_cache", {})
    original = job(description=(
        "Summer 2027 Software Engineering Intern. "
        "Applicants should be in the class of 2028. Mentorship and production work."
    ))
    annotate(original)
    saved = dict(original.internship_eligibility)
    rehydrated = Job(company=original.company, title=original.title, url=original.url,
                     source=original.source, locations=original.locations,
                     internship_eligibility=saved)
    annotate(rehydrated)
    assert rehydrated.internship_eligibility["status"] == "explicit"
    assert rehydrated.internship_eligibility["graduation_start"] == "2028-01-01"
    assert rehydrated.internship_eligibility["work_quality"]["points"] > 0


def test_curated_internship_source_parsers_are_lane_tagged():
    simplify = [{
        "active": True, "is_visible": True, "company_name": "Acme",
        "title": "Machine Learning Intern", "url": "https://acme.test/1",
        "locations": ["Remote, US"], "date_posted": int(time.time()),
    }]
    with patch.object(aggregators, "get_json", return_value=simplify):
        simplified = aggregators.fetch_simplify_internship()
    assert simplified[0].profile == "internship"
    assert simplified[0].source == "simplify_internship"

    speedy = (
        '| <a href="https://acme.test"><strong>Acme</strong></a> | '
        'Software Engineering Intern | Remote | $40/hr | '
        '<a href="https://acme.test/2">Apply</a> | 1d |\n'
    )
    with patch.object(aggregators, "get_text", return_value=speedy):
        speedy_jobs = aggregators.fetch_speedyapply_internship()
    assert len(speedy_jobs) == 1
    assert speedy_jobs[0].profile == "internship"

    dreamwork = [{
        "company": "Acme", "title": "Data Science Intern",
        "url": "https://acme.test/3", "location": "Boston, MA",
        "postedAt": "2026-08-01T00:00:00Z",
    }]
    with patch.object(aggregators, "get_json", return_value=dreamwork):
        dreamwork_jobs = aggregators.fetch_dreamwork_internship()
    assert dreamwork_jobs[0].profile == "internship"
    assert dreamwork_jobs[0].source == "dreamwork_internship"


def test_new_offcycle_sources_keep_direct_links_and_source_terms():
    applyguy = {"jobs": [{
        "active": True, "company": "Acme", "title": "Software Engineering Intern",
        "locations": ["New York, NY"], "season": "Winter 2027",
        "postedAt": "2026-10-07T00:00:00Z",
        "listingUrl": "https://acme.test/careers/1", "url": "https://applyguy.test/1",
    }]}
    with patch.object(aggregators, "get_json", return_value=applyguy):
        applyguy_jobs = aggregators.fetch_applyguy_internship()
    assert len(applyguy_jobs) == 1
    assert applyguy_jobs[0].url == "https://acme.test/careers/1"
    assert applyguy_jobs[0].alternate_urls == ["https://applyguy.test/1"]
    assert applyguy_jobs[0].internship_eligibility["source_term"] == "Winter 2027"

    offcycle = (
        "| Acme | Software Engineer Co-op | Remote | Spring 2027 | BS | "
        "[Apply](https://acme.test/careers/2) | Oct 8 |\n"
    )
    with patch.object(aggregators, "get_text", return_value=offcycle):
        offcycle_jobs = aggregators.fetch_aprameyak_offcycle()
    assert len(offcycle_jobs) == 1
    assert offcycle_jobs[0].url == "https://acme.test/careers/2"
    assert offcycle_jobs[0].internship_eligibility["source_term"] == "Spring 2027"
    annotate(offcycle_jobs[0])
    assert offcycle_jobs[0].internship_eligibility["term_start"] == "2027-01-01"


def test_direct_ats_internship_metadata_is_preserved(monkeypatch):
    monkeypatch.setenv("RADAR_PROFILE", "internship")
    lever_payload = [{
        "text": "Software Engineer", "hostedUrl": "https://acme.test/4",
        "categories": {"commitment": "Internship", "location": "Remote"},
    }]
    with patch.object(ats, "get_json", return_value=lever_payload):
        lever_job = ats.fetch_lever({"name": "Acme", "token": "acme"})[0]
    annotate(lever_job)
    assert lever_job.internship_eligibility["source_signal"] is True
    assert lever_job.internship_eligibility["internship_signal"] == "source_only"
    assert gates(lever_job)[0:2] == (True, False)


def test_state_namespace_isolated_by_lane(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    monkeypatch.setenv("RADAR_PROFILE", "internship")
    state.save("jobs.json", {"intern": True})
    assert (tmp_path / "intern_jobs.json").exists()
    assert state.jobs() == {"intern": True}

    monkeypatch.setenv("RADAR_PROFILE", "new_grad")
    state.save("jobs.json", {"new_grad": True})
    assert state.jobs() == {"new_grad": True}
    assert (tmp_path / "jobs.json").exists()
    assert (tmp_path / "intern_jobs.json").read_text()


def test_internship_email_defaults_on_and_uses_separate_surface(tmp_path, monkeypatch):
    from radar import board

    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    monkeypatch.setenv("RADAR_PROFILE", "internship")
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setattr("radar.board._existing_marker_ids", lambda *args, **kwargs: set())
    history = [{"id": "i" * 16, "company": "Acme", "title": "SWE Intern",
                "url": "https://acme.test/5", "score": 95,
                "alerted_at": int(time.time()) - 3600, "locations": ["Remote"],
                "internship_eligibility": {"term_start": "2027-01-01"}}]
    response = type("Response", (), {
        "raise_for_status": lambda self: None,
        "json": lambda self: {"html_url": "https://github.test/internships"},
    })()
    monkeypatch.setenv("RADAR_EMAIL_BATCH_MIN", "1")
    monkeypatch.setenv("RADAR_EMAIL_BATCH_MAX_WAIT_HOURS", "0")
    sent = {}
    def capture_post(*args, **kwargs):
        sent.update(kwargs.get("json") or {})
        return response
    monkeypatch.setattr("radar.board.requests.post", capture_post)
    assert board.email_enabled() is True
    url = board.post_email_batch(history)
    assert url == "https://github.test/internships"
    assert "Internship batch" in sent["title"]
    assert "new internship roles" in sent["body"]
    payload = state.load("notification_state.json", {})
    assert payload["email_batch_sent_ids"] == ["i" * 16]

    state.save_shared("notification_preferences.json", {
        "new_grad_email": True, "internship_email": False,
    })
    monkeypatch.setattr("radar.board.requests.post", lambda *args, **kwargs:
                        pytest.fail("an explicit off preference must suppress internship email"))
    assert board.email_enabled() is False


def test_internship_email_batch_only_includes_spring_winter_2027(tmp_path, monkeypatch):
    from radar import board

    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    monkeypatch.setenv("RADAR_PROFILE", "internship")
    monkeypatch.setenv("RADAR_EMAIL_BATCH_MIN", "1")
    monkeypatch.setenv("RADAR_EMAIL_BATCH_MAX_WAIT_HOURS", "0")
    now = int(time.time())
    history = [
        {"id": "spring", "company": "Spring Co", "title": "SWE Intern",
         "score": 95, "alerted_at": now - 3600,
         "internship_eligibility": {"term_start": "2027-01-01"}},
        {"id": "summer", "company": "Summer Co", "title": "Summer 2027 SWE Intern",
         "score": 99, "alerted_at": now - 1800,
         "internship_eligibility": {"term_start": "2027-06-01"}},
        {"id": "unknown", "company": "Unknown Co", "title": "SWE Intern",
         "score": 98, "alerted_at": now - 900,
         "internship_eligibility": {"term_start": None}},
    ]
    rows = board.email_batch_rows(history, set(), now, jobs_state=None)
    assert [row["id"] for row in rows] == ["spring"]


def test_internship_email_v2_terms_override_stale_source_term():
    from radar.board import _spring_winter_2027_internship

    summer_only = {
        "title": "Summer 2027 SWE Intern",
        "internship_eligibility": {
            "evidence_version": 2,
            "term_options": ["2027-06-01"],
            "source_term": "Spring 2027 / Summer 2027",
        },
    }
    assert _spring_winter_2027_internship(summer_only) is False
