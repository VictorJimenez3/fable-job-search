import json
from pathlib import Path

import pytest

from radar.application_agent import (
    add_issue,
    apply_confirmation,
    create_session,
    get_session,
    infer_category,
    list_issues,
    plan_form,
    provider_for_url,
    record_event,
    save_answer,
    verify_submission_page,
)


def job():
    return {
        "id": "job-1",
        "company": "Example Co",
        "title": "Software Engineer",
        "url": "https://boards.greenhouse.io/example/jobs/1",
    }


def test_top_five_provider_and_sensitive_categories_are_deterministic():
    assert provider_for_url("https://acme.wd5.myworkdayjobs.com/en-US/acme/job/1") == "workday"
    assert provider_for_url("https://job-boards.greenhouse.io/acme/jobs/1") == "greenhouse"
    assert provider_for_url("https://jobs.lever.co/acme/1") == "lever"
    assert provider_for_url("https://jobs.ashbyhq.com/acme/1") == "ashby"
    assert provider_for_url("https://jobs.smartrecruiters.com/acme/1") == "smartrecruiters"
    assert infer_category({"label": "Work authorization", "type": "select"}) == "work_authorization"
    assert infer_category({"label": "Tell us why you want this role", "type": "textarea"}) == "essay"
    assert infer_category({"label": "I certify that the information is accurate", "type": "checkbox"}) == "attestation"
    assert infer_category({"label": "LinkedIn", "type": "checkbox"}) == "attestation"
    assert infer_category({"label": "Yes", "group_question": "Are you legally authorized to work in the US?", "type": "button"}) == "work_authorization"
    assert infer_category({"label": "He/Him", "group_question": "Pronouns", "type": "button"}) == "gender"
    assert infer_category({"label": "Yes", "group_question": "Do you have experience with LLMs?", "type": "button"}) == "llm_experience"
    assert infer_category({"label": "Yes", "group_question": "Can you work from our offices on Anchor Days?", "type": "button"}) == "work_schedule"
    assert infer_category({"label": "School", "name": "a-very-long-generated-field-name-that-must-not-turn-school-into-an-essay", "type": "text"}) == "education"
    assert infer_category({"label": "Resume", "type": "file"}) == "resume_file"
    assert infer_category({"label": "Cover Letter", "type": "file"}) == "cover_letter_file"
    assert infer_category({"label": "Official transcript", "type": "file"}) == "supporting_file"


def test_required_cover_letter_file_is_not_treated_as_resume(tmp_path: Path):
    session = create_session(tmp_path, job())
    result = plan_form(
        tmp_path,
        session["session_id"],
        job()["url"],
        [
            {"field_id": "resume", "label": "Resume", "type": "file", "required": True, "value": "tailored.pdf"},
            {"field_id": "cover", "label": "Cover Letter", "type": "file", "required": True, "value": ""},
        ],
    )
    assert result["state"] == "blocked"
    assert [item["category"] for item in result["blockers"]] == ["cover_letter_file"]
    assert "separate cover-letter file" in result["blockers"][0]["reason"]


def test_approved_answers_fill_and_unknown_fields_pause(tmp_path: Path):
    save_answer(tmp_path, "Email address", "victor@example.com", category="email")
    session = create_session(tmp_path, job())
    result = plan_form(
        tmp_path,
        session["session_id"],
        job()["url"],
        [
            {"field_id": "email", "label": "Email address", "type": "email", "required": True},
            {"field_id": "auth", "label": "Are you legally authorized to work?", "type": "select", "required": True},
        ],
    )
    assert result["state"] == "blocked"
    assert result["fills"][0]["value"] == "victor@example.com"
    assert result["blockers"][0]["category"] == "work_authorization"


def test_non_error_application_progress_message_is_persisted(tmp_path: Path):
    session = create_session(tmp_path, job())
    result = record_event(
        tmp_path,
        session["session_id"],
        "filling",
        message="Resume uploaded; waiting for employer validation.",
    )
    assert result["state"] == "filling"
    stored = get_session(tmp_path, session["session_id"])
    assert stored["last_message"] == "Resume uploaded; waiting for employer validation."
    assert stored["last_error"] == ""


def test_approved_choice_answers_fill_attestations_and_optional_demographics(tmp_path: Path):
    save_answer(tmp_path, "Male", "Male", category="gender")
    save_answer(tmp_path, "I decline to self-identify", "I decline to self-identify", category="disability")
    save_answer(tmp_path, "Yes", "Yes", category="work_authorization")
    session = create_session(tmp_path, job())
    result = plan_form(
        tmp_path,
        session["session_id"],
        job()["url"],
        [
            {"field_id": "gender", "label": "Male", "group_question": "Gender", "type": "radio", "required": False, "group_options": ["Male", "Female"]},
            {"field_id": "disability", "label": "I decline to self-identify", "group_question": "Disability status", "type": "button", "required": False, "group_options": ["I decline to self-identify", "Yes"]},
            {"field_id": "auth", "label": "Yes", "group_question": "Are you legally authorized to work in the US?", "type": "button", "required": True, "group_options": ["Yes", "No"]},
        ],
    )
    assert result["state"] == "filling"
    assert {item["category"] for item in result["fills"]} == {"gender", "disability", "work_authorization"}
    assert not result["blockers"]


def test_category_specific_answers_fill_repeated_yes_groups_and_text_fields(tmp_path: Path):
    save_answer(tmp_path, "Anchor Days", "Yes", category="work_schedule", variants=["Can you commit to working from one of our offices on Anchor Days each week?"])
    save_answer(tmp_path, "Do you have experience with LLMs?", "Yes", category="llm_experience")
    save_answer(tmp_path, "Have you built a personal project using LLMs?", "Yes", category="llm_experience")
    save_answer(tmp_path, "Current Location", "Newark, NJ", category="location", variants=["Start typing..."])
    save_answer(tmp_path, "Graduation Date", "May 2027", category="education", variants=["Pick date..."])
    save_answer(tmp_path, "Please describe your AI experience.", "- Built an agentic LLM proof of concept.", category="essay")
    session = create_session(tmp_path, job())
    result = plan_form(
        tmp_path,
        session["session_id"],
        job()["url"],
        [
            {"field_id": "anchor", "label": "Yes", "group_question": "Can you commit to working from one of our offices on Anchor Days each week?", "type": "button", "required": True, "group_options": ["Yes", "No"]},
            {"field_id": "llm", "label": "Yes", "group_question": "Do you have experience with LLMs?", "type": "button", "required": True, "group_options": ["Yes", "No"]},
            {"field_id": "project", "label": "Yes", "group_question": "Have you built a personal project using LLMs?", "type": "button", "required": True, "group_options": ["Yes", "No"]},
            {"field_id": "location", "label": "Start typing...", "type": "combobox", "required": True},
            {"field_id": "graduation", "label": "Pick date...", "type": "text", "required": True},
            {"field_id": "ai", "label": "Please describe your AI experience.", "type": "textarea", "required": True},
        ],
    )
    assert result["state"] == "filling"
    assert {item["field_id"] for item in result["fills"]} == {"anchor", "llm", "project", "location", "graduation", "ai"}
    assert not result["blockers"]


def test_unrelated_yes_no_answers_cannot_fill_a_generic_attestation(tmp_path: Path):
    save_answer(tmp_path, "Yes", "Yes", category="work_authorization")
    save_answer(tmp_path, "None", "None", category="sponsorship", variants=["No"])
    session = create_session(tmp_path, job())
    question = "I can attend the required coordination hours each week. *"
    result = plan_form(
        tmp_path,
        session["session_id"],
        job()["url"],
        [
            {"field_id": "coord-yes", "label": "Yes", "group_question": question, "type": "button", "required": True, "group_key": "coordination", "group_options": ["Yes", "No"]},
            {"field_id": "coord-no", "label": "No", "group_question": question, "type": "button", "required": True, "group_key": "coordination", "group_options": ["Yes", "No"]},
        ],
    )
    assert result["fills"] == []
    assert result["state"] == "blocked"
    assert len(result["blockers"]) == 1
    assert result["blockers"][0]["label"] == question


def test_review_collapses_choice_siblings_and_duplicate_resume_inputs(tmp_path: Path):
    save_answer(tmp_path, "Male", "Male", category="gender")
    session = create_session(tmp_path, job())
    result = plan_form(
        tmp_path,
        session["session_id"],
        job()["url"],
        [
            {"field_id": "gender-male", "name": "gender", "label": "Male", "group_question": "How would you describe your gender identity?", "type": "radio", "required": False, "group_options": ["Male", "Female"]},
            {"field_id": "gender-female", "name": "gender", "label": "Female", "group_question": "How would you describe your gender identity?", "type": "radio", "required": False, "group_options": ["Male", "Female"]},
            {"field_id": "resume-empty", "label": "", "category": "resume_file", "type": "file", "required": False, "value": ""},
            {"field_id": "resume", "label": "Resume", "category": "resume_file", "type": "file", "required": True, "value": "tailored.pdf"},
        ],
        final=True,
    )
    assert result["state"] == "awaiting_confirmation"
    fields = result["review"]["fields"]
    assert len(fields) == 2
    gender = next(field for field in fields if field["category"] == "gender")
    resume = next(field for field in fields if field["category"] == "resume_file")
    assert gender["label"] == "How would you describe your gender identity?"
    assert gender["value"] == "Male"
    assert gender["field_ids"] == ["gender-male", "gender-female"]
    assert resume["label"] == "Resume"
    assert resume["value"] == "tailored.pdf"


def test_race_fallback_is_used_only_when_hispanic_option_is_absent(tmp_path: Path):
    save_answer(tmp_path, "Hispanic or Latino", "Hispanic or Latino", category="race_ethnicity")
    save_answer(
        tmp_path,
        "Black or African American",
        "Black or African American",
        category="race_ethnicity",
        fallback_for=["Hispanic or Latino", "Hispanic", "Latino"],
    )
    session = create_session(tmp_path, job())
    fields = [
        {"field_id": "hispanic", "label": "Hispanic or Latino", "group_question": "Race/Ethnicity", "type": "radio", "group_options": ["Hispanic or Latino", "Black or African American"]},
        {"field_id": "black", "label": "Black or African American", "group_question": "Race/Ethnicity", "type": "radio", "group_options": ["Hispanic or Latino", "Black or African American"]},
    ]
    result = plan_form(tmp_path, session["session_id"], job()["url"], fields)
    assert [item["field_id"] for item in result["fills"]] == ["hispanic"]

    session = create_session(tmp_path, job())
    result = plan_form(
        tmp_path,
        session["session_id"],
        job()["url"],
        [{**fields[1], "group_options": ["Black or African American", "White"]}],
    )
    assert result["fills"][0]["field_id"] == "black"


def test_location_select_all_answer_fills_each_relocation_checkbox(tmp_path: Path):
    save_answer(tmp_path, "All applicable locations", "all", category="location", select_all=True)
    session = create_session(tmp_path, job())
    result = plan_form(
        tmp_path,
        session["session_id"],
        job()["url"],
        [
            {"field_id": "ny", "label": "New York, NY", "group_question": "Which locations are you willing to relocate to?", "type": "checkbox", "group_options": ["New York, NY", "San Francisco, CA"]},
            {"field_id": "sf", "label": "San Francisco, CA", "group_question": "Which locations are you willing to relocate to?", "type": "checkbox", "group_options": ["New York, NY", "San Francisco, CA"]},
        ],
    )
    assert [item["field_id"] for item in result["fills"]] == ["ny", "sf"]


def test_selected_sponsorship_radio_suppresses_alternative_option_blockers(tmp_path: Path):
    save_answer(
        tmp_path, "None", "None", category="sponsorship",
        variants=["No", "No sponsorship", "I do not require sponsorship"],
    )
    session = create_session(tmp_path, job())
    options = ["OPT", "H1B", "TN", "None", "Other"]
    fields = [
        {
            "field_id": f"visa-{index}", "name": "visa-status", "label": option,
            "group_question": "Will you require sponsorship?", "type": "radio",
            "required": False, "group_options": options,
        }
        for index, option in enumerate(options)
    ]

    result = plan_form(tmp_path, session["session_id"], job()["url"], fields)

    assert [item["field_id"] for item in result["fills"]] == ["visa-3"]
    assert result["blockers"] == []
    assert result["optional_review"] == []


def test_radio_group_fills_only_the_approved_option(tmp_path: Path):
    save_answer(
        tmp_path, "Anchor Days", "Yes", category="work_schedule",
        variants=["Can you commit to Anchor Days each week?"],
    )
    session = create_session(tmp_path, job())
    fields = [
        {
            "field_id": f"anchor-{option.lower()}", "name": "anchor-days",
            "label": option, "group_question": "Can you commit to Anchor Days each week?",
            "type": "radio", "required": True, "group_options": ["Yes", "No"],
        }
        for option in ("Yes", "No")
    ]

    result = plan_form(tmp_path, session["session_id"], job()["url"], fields)

    assert [item["field_id"] for item in result["fills"]] == ["anchor-yes"]
    assert result["blockers"] == []


def test_sensitive_decline_answer_does_not_cross_demographic_categories(tmp_path: Path):
    save_answer(tmp_path, "Prefer not to say", "Prefer not to say", category="disability")
    save_answer(tmp_path, "Male", "Male", category="gender")
    session = create_session(tmp_path, job())
    fields = [
        {"field_id": "male", "name": "gender", "label": "Male", "group_question": "Gender", "type": "radio", "group_options": ["Male", "Prefer not to say"]},
        {"field_id": "decline", "name": "gender", "label": "Prefer not to say", "group_question": "Gender", "type": "radio", "group_options": ["Male", "Prefer not to say"]},
    ]

    result = plan_form(tmp_path, session["session_id"], job()["url"], fields)

    assert [item["field_id"] for item in result["fills"]] == ["male"]


def test_canonical_resume_seeds_deterministic_profile_fields(tmp_path: Path):
    resume = tmp_path / "CV" / "immutable" / "VictorJimenezResume.tex"
    resume.parent.mkdir(parents=True)
    resume.write_text(
        r"""% Based off of: https://github.com/sb2nov/resume
        \textbf{\Huge \scshape Victor Jimenez}
        \href{mailto:victor@example.com}{victor@example.com}
        (201) 555-0100
        \href{https://www.linkedin.com/in/vmj3}{linkedin.com/in/vmj3}
        \href{https://github.com/VictorJimenez3}{github.com/VictorJimenez3}
        {\large New Jersey Institute of Technology}{\large Newark, NJ}""",
        encoding="utf-8",
    )
    session = create_session(tmp_path, job())
    result = plan_form(
        tmp_path,
        session["session_id"],
        job()["url"],
        [
            {"field_id": "name", "label": "Full Name", "type": "text", "required": True},
            {"field_id": "email", "label": "Email", "type": "email", "required": True},
            {"field_id": "phone", "label": "Phone", "type": "tel", "required": True},
            {"field_id": "essay", "label": "Why this role?", "type": "textarea", "required": True},
        ],
    )
    assert {item["value"] for item in result["fills"]} == {
        "Victor Jimenez", "victor@example.com", "(201) 555-0100",
    }
    assert result["blockers"][0]["category"] == "essay"


def test_canonical_resume_repairs_a_stale_seeded_profile_value(tmp_path: Path):
    resume = tmp_path / "CV" / "immutable" / "VictorJimenezResume.tex"
    resume.parent.mkdir(parents=True)
    resume.write_text(
        r"""% Based off of: https://github.com/sb2nov/resume
        \textbf{\Huge \scshape Victor Jimenez}
        \href{https://github.com/VictorJimenez3}{github.com/VictorJimenez3}""",
        encoding="utf-8",
    )
    create_session(tmp_path, job())
    path = tmp_path / "CV" / ".resume_studio" / "application_agent.json"
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored["context"]["answers"]["canonical-github"]["value"] = "https://github.com/sb2nov"
    path.write_text(json.dumps(stored), encoding="utf-8")

    result = plan_form(
        tmp_path,
        create_session(tmp_path, job())["session_id"],
        job()["url"],
        [{"field_id": "github", "label": "Github Link", "type": "text", "required": True}],
    )

    assert result["fills"][0]["value"] == "https://github.com/VictorJimenez3"


def test_final_review_is_explicit_and_page_bound(tmp_path: Path):
    save_answer(tmp_path, "Email address", "victor@example.com", category="email")
    save_answer(tmp_path, "Why do you want this role?", "I build reliable systems.", category="essay")
    session = create_session(tmp_path, job())
    fields = [
        {"field_id": "email", "label": "Email", "type": "email", "required": True},
        {"field_id": "why", "label": "Why do you want this role?", "type": "textarea", "required": True},
        {"field_id": "submit", "label": "Submit application", "type": "button", "is_submit": True},
    ]
    result = plan_form(tmp_path, session["session_id"], job()["url"], fields, final=True)
    assert result["state"] == "awaiting_confirmation"
    assert result["review"]["fields"][0]["value"] == "victor@example.com"
    assert result["review"]["fields"][1]["value"] == "I build reliable systems."
    assert result["review"]["page_fingerprint"]

    apply_confirmation(
        tmp_path,
        session["session_id"],
        result["review"]["review_hash"],
        result["review"]["nonce"],
        result["review"]["page_fingerprint"],
    )
    assert get_session(tmp_path, session["session_id"])["state"] == "submitting"
    live_fields = [
        {**fields[0], "value": "victor@example.com"},
        {**fields[1], "value": "I build reliable systems."},
        fields[2],
    ]
    assert verify_submission_page(tmp_path, session["session_id"], job()["url"], live_fields)["ok"] is True

    changed_value = [live_fields[0], {**live_fields[1], "value": "A different answer"}, fields[2]]
    with pytest.raises(ValueError, match="field values changed"):
        verify_submission_page(tmp_path, session["session_id"], job()["url"], changed_value)

    with pytest.raises(ValueError, match="already been consumed"):
        apply_confirmation(
            tmp_path,
            session["session_id"],
            result["review"]["review_hash"],
            result["review"]["nonce"],
            result["review"]["page_fingerprint"],
        )

    changed = [dict(live_fields[0]), {**live_fields[1], "label": "Different essay"}, fields[2]]
    with pytest.raises(ValueError, match="page changed"):
        verify_submission_page(tmp_path, session["session_id"], job()["url"], changed)


def test_identical_review_rescan_is_rejected_until_the_page_changes(tmp_path: Path):
    save_answer(tmp_path, "Email", "victor@example.com", category="email")
    session = create_session(tmp_path, job())
    fields = [
        {"field_id": "email", "label": "Email", "type": "email", "required": True, "value": ""},
        {"field_id": "submit", "label": "Submit application", "type": "button", "is_submit": True},
    ]
    first = plan_form(tmp_path, session["session_id"], job()["url"], fields, final=True)
    assert first["state"] == "awaiting_confirmation"
    with pytest.raises(ValueError, match="review is already current"):
        plan_form(tmp_path, session["session_id"], job()["url"], fields, final=True)
    changed = [{**fields[0], "value": "victor@example.com"}, fields[1]]
    with pytest.raises(ValueError, match="explicit rescan"):
        plan_form(tmp_path, session["session_id"], job()["url"], changed, final=True)
    record_event(tmp_path, session["session_id"], "filling", message="Owner requested a fresh application-page scan.")
    rebuilt = plan_form(tmp_path, session["session_id"], job()["url"], changed, final=True)
    assert rebuilt["state"] == "awaiting_confirmation"


def test_owner_entered_essay_and_checked_attestation_are_reviewable(tmp_path: Path):
    session = create_session(tmp_path, job())
    fields = [
        {"field_id": "why", "label": "Why do you want this role?", "type": "textarea", "required": True, "value": "I want to build reliable systems."},
        {"field_id": "agree", "label": "I certify that the information is accurate", "type": "checkbox", "required": True, "value": True},
        {"field_id": "submit", "label": "Submit application", "type": "button", "is_submit": True},
    ]
    result = plan_form(tmp_path, session["session_id"], job()["url"], fields, final=True)
    assert result["state"] == "awaiting_confirmation"
    assert not result["blockers"]
    assert [field["value"] for field in result["review"]["fields"]] == [
        "I want to build reliable systems.", "checked",
    ]


def test_unchecked_attestation_stays_a_blocker_until_owner_handles_it(tmp_path: Path):
    session = create_session(tmp_path, job())
    result = plan_form(
        tmp_path,
        session["session_id"],
        job()["url"],
        [{"field_id": "agree", "label": "I certify that the information is accurate", "type": "checkbox", "required": True, "value": False}],
        final=True,
    )
    assert result["state"] == "blocked"
    assert "Check this attestation" in result["blockers"][0]["reason"]


def test_issue_ledger_is_sanitized_and_local(tmp_path: Path):
    session = create_session(tmp_path, job())
    issue = add_issue(tmp_path, session["session_id"], "selector", "Submit control moved", "Submit", job()["url"], "abc123", "button")
    assert issue["provider"] == "greenhouse"
    assert list_issues(tmp_path)[0]["issue_id"] == issue["issue_id"]
    assert (tmp_path / "CV" / ".resume_studio" / "application_agent.json").exists()
