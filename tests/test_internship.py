import time
from unittest.mock import patch

import pytest

from radar import state
from radar.internship import analyze, annotate, gates, match, score
from radar.models import Job
from radar.sources import aggregators, ats


def job(title="Software Engineering Intern", *, source="greenhouse", description="",
        locations=None, **kwargs):
    return Job(company="Acme", title=title, url="https://example.test/job/1",
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
    assert gates(curated)[0:2] == (True, True)

    assert gates(job(title="Senior Software Engineering Intern"))[0] is False
    assert gates(job(locations=["London, United Kingdom"]))[0] is False


def test_internship_score_has_auditable_reasons(monkeypatch):
    monkeypatch.setenv("RADAR_PROFILE", "internship")
    from radar import config
    monkeypatch.setattr(config, "_profile_cache", {})
    posting = job(description="Current undergraduate students graduating in 2028.", remote=True)
    annotate(posting)
    score(posting, int(time.time()))
    assert posting.score > 0
    assert any("role:swe" in reason for reason in posting.score_reasons)
    assert any("graduation eligibility" in reason for reason in posting.score_reasons)
    assert any("remote" in reason for reason in posting.score_reasons)


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
    assert gates(lever_job)[0:2] == (True, True)


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


def test_internship_email_is_opt_in_and_uses_separate_surface(tmp_path, monkeypatch):
    from radar import board

    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    monkeypatch.setenv("RADAR_PROFILE", "internship")
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    history = [{"id": "i" * 16, "company": "Acme", "title": "SWE Intern",
                "url": "https://acme.test/5", "score": 95,
                "alerted_at": int(time.time()) - 3600, "locations": ["Remote"]}]
    monkeypatch.setattr("radar.board.requests.post", lambda *args, **kwargs:
                        pytest.fail("internship email must be opt-in"))
    assert board.post_email_batch(history) is None

    state.save_shared("notification_preferences.json", {
        "new_grad_email": True, "internship_email": True,
    })
    response = type("Response", (), {
        "raise_for_status": lambda self: None,
        "json": lambda self: {"html_url": "https://github.test/internships"},
    })()
    monkeypatch.setattr("radar.board.requests.post", lambda *args, **kwargs: response)
    monkeypatch.setenv("RADAR_EMAIL_BATCH_MIN", "1")
    monkeypatch.setenv("RADAR_EMAIL_BATCH_MAX_WAIT_HOURS", "0")
    url = board.post_email_batch(history)
    assert url == "https://github.test/internships"
    payload = state.load("notification_state.json", {})
    assert payload["email_batch_sent_ids"] == ["i" * 16]
