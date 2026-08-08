from radar.discovery import extract, harvest, key, seed_registry
from radar.main import _pm_backfill_ids, _select_companies
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


def test_pm_harvest_is_prioritized_and_marks_direct_company_interest():
    reg = {}
    jobs = [
        Job(company="Technical Co", title="Software Engineer, New Grad",
            url="https://boards.greenhouse.io/technicalco/jobs/1", source="simplify"),
        Job(company="Product Co", title="Associate Product Manager, New Grad",
            url="https://boards.greenhouse.io/productco/jobs/2", source="zapply_pm"),
    ]
    assert harvest(reg, jobs, max_new=1) == 1
    assert "greenhouse:productco" in reg
    assert reg["greenhouse:productco"]["pm_interest"] is True
    assert reg["greenhouse:productco"]["pm_sources"] == ["zapply_pm"]


def test_pm_interest_companies_win_the_direct_polling_cap():
    entries = [
        {"name": "Technical Co", "status": "active", "origin": "seed",
         "sector": "ai_lab", "last_ok": 100},
        {"name": "Product Co", "status": "active", "origin": "harvest:zapply_pm",
         "sector": "", "last_ok": 1, "pm_interest": True},
    ]
    registry = {str(i): entry for i, entry in enumerate(entries)}
    assert _select_companies(registry, 1)[0]["name"] == "Product Co"


def test_pm_direct_backfill_is_bounded_to_query_cap():
    entries = [
        {"name": "PM Co 1", "ats": "workday", "pm_interest": True},
        {"name": "PM Co 2", "ats": "phenom", "pm_interest": True},
        {"name": "PM Co 3", "ats": "workday", "pm_interest": True},
        {"name": "Greenhouse Co", "ats": "greenhouse", "pm_interest": True},
    ]
    got = _pm_backfill_ids(entries, 2)
    assert id(entries[0]) in got
    assert id(entries[1]) in got
    assert id(entries[2]) not in got
    assert id(entries[3]) not in got


def test_multiple_fanatics_department_boards_can_be_seeded():
    reg = {}
    boards = [
        {"name": "Fanatics", "ats": "greenhouse", "token": token, "sector": "sports"}
        for token in ("fanaticsinc", "fanaticsfbg", "fanaticscommerce", "fanaticscollectibles")
    ]
    assert seed_registry(reg, boards) == 4
    assert {e["token"] for e in reg.values()} == {b["token"] for b in boards}


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
