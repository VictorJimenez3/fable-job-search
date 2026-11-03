from radar.eligibility import experience_evidence, new_grad_evidence


def test_preferred_experience_is_not_a_required_floor():
    evidence = experience_evidence(
        "One year of experience preferred; no experience required."
    )

    assert evidence["required_floor"] == 0
    assert evidence["preferred_floor"] == 1
    assert evidence["conflict"] is False
    assert "preferred" in evidence["note"]


def test_required_and_preferred_experience_are_kept_separate():
    evidence = experience_evidence(
        "0-2 years experience required. 3+ years of experience is preferred."
    )

    assert evidence["required_floor"] == 0
    assert evidence["preferred_floor"] == 3
    assert evidence["conflict"] is False


def test_no_prior_professional_experience_is_a_zero_floor():
    evidence = experience_evidence("No prior professional experience required.")

    assert evidence["required_floor"] == 0
    assert evidence["required_floor_explicit"] is False


def test_company_age_and_benefit_tenure_are_not_experience_floors():
    assert experience_evidence(
        "Our company has led innovation for 30 years. Benefits vest after 3 years."
    )["required_floor"] is None


def test_company_duration_does_not_inherit_experience_from_prior_sentence():
    assert experience_evidence(
        "Experience with Python. 5 years in business."
    )["required_floor"] is None
    assert experience_evidence(
        "Experience with Python. 5 years of professional experience required."
    )["required_floor"] == 5


def test_required_and_preferred_headings_survive_multiline_parsing():
    evidence = experience_evidence(
        "Required Qualifications:\n3+ years of experience.\n"
        "Preferred Qualifications:\n5+ years of experience."
    )

    assert evidence["required_floor"] == 3
    assert evidence["preferred_floor"] == 5
    assert evidence["conflict"] is False


def test_degree_experience_alternatives_are_not_collapsed_to_max_floor():
    evidence = experience_evidence(
        "Bachelor's degree and 2+ years OR Master's degree and 0 years experience."
    )

    assert evidence["alternative"] is True
    assert evidence["required_floor"] is None
    assert evidence["conflict"] is False
    assert "alternative" in evidence["note"]


def test_conflicting_required_experience_is_review_only_evidence():
    evidence = experience_evidence(
        "0-2 years of experience required. Minimum 3 years required."
    )

    assert evidence["required_floor"] == 3
    assert evidence["conflict"] is True
    assert "conflicting" in evidence["note"]


def test_mismatched_class_year_is_not_verified_new_grad_evidence():
    evidence = new_grad_evidence(
        "Software Engineer, Class of 2025", candidate_grad_year=2027
    )

    assert evidence["status"] == "review"
    assert evidence["cohort_conflict"] is True
    assert "cohort" in evidence["reason"]


def test_adjacent_class_year_is_review_only_for_candidate_cohort():
    evidence = new_grad_evidence(
        "Software Engineer, Class of 2028", candidate_grad_year=2027
    )

    assert evidence["status"] == "review"
    assert evidence["cohort_conflict"] is True


def test_cohort_window_including_candidate_is_not_a_conflict():
    evidence = new_grad_evidence(
        "Software Engineer, Class of 2026 or 2027", candidate_grad_year=2027
    )

    assert evidence["cohort_years"] == [2026, 2027]
    assert evidence["cohort_conflict"] is False


def test_senior_mentors_do_not_make_a_new_grad_role_senior():
    evidence = new_grad_evidence(
        "Software Engineer, New Grad",
        "You will be mentored by senior-level engineers.",
    )

    assert evidence["status"] == "verified"


def test_senior_teammates_do_not_make_a_new_grad_role_senior():
    evidence = new_grad_evidence(
        "Software Engineer, New Grad",
        "Work with a senior-level engineer on production systems.",
    )

    assert evidence["status"] == "verified"


def test_trusted_source_without_posting_evidence_is_review_only():
    evidence = new_grad_evidence(
        "Software Engineer", source="simplify", description=""
    )

    assert evidence["status"] == "review"
    assert evidence["source"] == "source-only"


def test_source_only_lead_cannot_promote_new_grad_title():
    evidence = new_grad_evidence(
        "Software Engineer, New Grad",
        source="public_feed",
        source_only=True,
    )

    assert evidence["status"] == "review"
    assert evidence["source"] == "source-only"


def test_explicit_title_is_verified_without_posting_body():
    evidence = new_grad_evidence("Software Engineer, New Grad", description="")

    assert evidence["status"] == "verified"
    assert evidence["source"] == "title"
