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


def test_hygiene_retries_prunes_and_parks():
    from radar.discovery import hygiene
    NOW = 10_000_000_000
    D = 86400
    registry = {
        # dead board → one fresh probe cycle a month
        "gh:deadco": {"name": "DeadCo", "status": "dead", "origin": "harvest:x",
                      "failures": 5, "last_ok": 0},
        # non-seed invalid, stamped 100 days ago → pruned
        "gh:ghostco": {"name": "GhostCo", "status": "invalid", "origin": "scout",
                       "invalidated_at": NOW - 100 * D, "last_ok": 0},
        # seed invalid → never pruned
        "gh:seedco": {"name": "SeedCo", "status": "invalid", "origin": "seed",
                      "invalidated_at": NOW - 400 * D, "last_ok": 0},
        # unstamped invalid → clock starts now, survives this pass
        "gh:newinv": {"name": "NewInv", "status": "invalid", "origin": "harvest:x",
                      "last_ok": 0},
        # duplicate employer: greenhouse produces, workday hasn't in 60d → parked
        "gh:acme": {"name": "Acme", "status": "active", "origin": "seed",
                    "last_ok": NOW - 1 * D},
        "wd:acme": {"name": "Acme", "status": "active", "origin": "harvest:x",
                    "last_ok": NOW - 60 * D},
        # duplicate where BOTH produce → untouched
        "gh:bigco": {"name": "BigCo", "status": "active", "origin": "seed",
                     "last_ok": NOW - 2 * D},
        "ph:bigco": {"name": "BigCo", "status": "active", "origin": "seed",
                     "last_ok": NOW - 3 * D},
    }
    stats = hygiene(registry, NOW)
    assert stats == {"dead_retried": 1, "invalid_pruned": 1, "dups_parked": 1}
    assert registry["gh:deadco"]["status"] == "new"
    assert registry["gh:deadco"]["probe_attempts"] == 0
    assert "gh:ghostco" not in registry
    assert registry["gh:seedco"]["status"] == "invalid"       # seeds stay
    assert registry["gh:newinv"]["invalidated_at"] == NOW      # clock started
    assert registry["wd:acme"]["status"] == "dup"
    assert registry["gh:acme"]["status"] == "active"
    assert registry["gh:bigco"]["status"] == registry["ph:bigco"]["status"] == "active"
    # idempotent within the month: dead retry doesn't loop
    stats2 = hygiene(registry, NOW)
    assert stats2 == {"dead_retried": 0, "invalid_pruned": 0, "dups_parked": 0}
