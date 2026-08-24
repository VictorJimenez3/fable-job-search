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
    assert infer_category({"label": "School", "name": "a-very-long-generated-field-name-that-must-not-turn-school-into-an-essay", "type": "text"}) == "education"


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
