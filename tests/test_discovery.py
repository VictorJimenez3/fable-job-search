from radar.discovery import extract, harvest, key, seed_registry
from radar.models import Job


def test_extract_greenhouse():
    assert extract("https://job-boards.greenhouse.io/altaresourcetechnologiesinc/jobs/4225005009") == \
        ("greenhouse", "altaresourcetechnologiesinc", {})
    assert extract("https://boards.greenhouse.io/stripe/jobs/123") == ("greenhouse", "stripe", {})


def test_extract_lever_ashby_smartrecruiters():
    assert extract("https://jobs.lever.co/plaid/abc-def") == ("lever", "plaid", {})
    assert extract("https://jobs.ashbyhq.com/openai/xyz") == ("ashby", "openai", {})
    assert extract("https://jobs.smartrecruiters.com/Visa/744000012345-swe") == \
        ("smartrecruiters", "Visa", {})


def test_extract_workday():
    got = extract("https://pmpediatrics.wd5.myworkdayjobs.com/PMPeds/job/Remote---United-States/AI-Engineer_R123")
    assert got == ("workday", "pmpediatrics", {"host": "wd5", "site": "PMPeds"})
    got = extract("https://nvidia.wd5.myworkdayjobs.com/en-US/nvidiaexternalcareersite/job/US-CA/SWE_JR1")
    assert got == ("workday", "nvidia", {"host": "wd5", "site": "nvidiaexternalcareersite"})


def test_extract_none_for_unknown():
    assert extract("https://careers.qualcomm.com/careers/job/446704474013") is None
    assert extract("") is None


def test_harvest_and_seed_registry():
    reg = {}
    n = seed_registry(reg, [{"name": "Stripe", "ats": "greenhouse", "token": "stripe", "sector": "big_tech"}])
    assert n == 1 and key("greenhouse", "stripe") in reg
    jobs = [
        Job(company="Plaid", title="SWE", url="https://jobs.lever.co/plaid/x", source="simplify"),
        Job(company="Plaid", title="SWE 2", url="https://jobs.lever.co/plaid/y", source="simplify"),
        Job(company="Stripe", title="SWE", url="https://boards.greenhouse.io/stripe/jobs/1", source="simplify"),
    ]
    added = harvest(reg, jobs)
    assert added == 1  # plaid once; stripe already seeded
    assert reg["lever:plaid"]["origin"] == "harvest:simplify"
    assert reg["lever:plaid"]["status"] == "new"
