from radar.internship import annotate, gates, score
from radar.models import Job


def test_non_us_and_off_role_internships_remain_reviewable_not_recommended(monkeypatch):
    monkeypatch.setenv("RADAR_PROFILE", "internship")
    for title, locations, remote in (
        ("Software Engineering Intern", ["Aarhus, Denmark"], False),
        ("Software Engineering Intern", ["Belgrade, Serbia"], False),
        ("Software Engineering Intern", ["Doha, Qatar"], False),
        ("Software Engineering Intern", ["Remote, Canada"], True),
        ("Electrician Intern", ["New York, NY"], False),
    ):
        job = Job(company="Databricks", title=title, url="https://example.test/jobs/123",
                  source="greenhouse", locations=locations, remote=remote)
        annotate(job)
        assert not gates(job)[1]
        score(job, 1_793_700_000)
        assert job.eligibility == "review"


def test_explicit_us_option_and_unspecified_remote_remain_eligible(monkeypatch):
    monkeypatch.setenv("RADAR_PROFILE", "internship")
    for locations in (["Toronto, Canada", "Remote, United States"], ["Remote"]):
        job = Job(company="Databricks", title="Software Engineering Intern",
                  url="https://example.test/jobs/123", source="greenhouse",
                  locations=locations, remote=True)
        annotate(job)
        assert gates(job)[1]
        score(job, 1_793_700_000)
        assert job.eligibility == "eligible"


def test_degree_specific_internship_titles_retain_bachelors_alternatives(monkeypatch):
    monkeypatch.setenv("RADAR_PROFILE", "internship")
    for title, eligible in (
        ("Product Management MBA Intern, Gaming - Summer 2027", False),
        ("Software Engineering Intern, MS, Summer 2027", False),
        ("Software Engineering Intern, BS/MS, Summer 2027", True),
        ("Software Engineering Intern, BS, Summer 2027", True),
    ):
        job = Job(company="NVIDIA", title=title, url="https://example.test/jobs/123",
                  source="workday", locations=["United States"])
        annotate(job)
        assert gates(job)[1] is eligible
        assert (job.internship_eligibility["degree_requirement"] == "graduate_only") is (not eligible)
