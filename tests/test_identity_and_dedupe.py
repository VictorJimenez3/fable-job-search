from radar.dedupe import (
    collapse_cross_source_jobs,
    collapse_jobs,
    collapse_location_variants,
    remap_entry_ids,
    remap_web_jobs,
    resolve_alias,
)
from radar.identity import canonical_url, posting_key
from radar.main import _existing_posting_ids, _state_job_identity, _unique_discovered_jobs
from radar.models import Job


def test_canonical_url_removes_tracking_but_keeps_job_parameters():
    assert canonical_url(
        "HTTPS://Example.com/role/?utm_source=email&ref=board&gh_jid=123#apply"
    ) == "https://example.com/role?gh_jid=123"


def test_canonical_url_rejects_malformed_authority_without_raising():
    assert canonical_url("https://[broken.example/jobs/123") == ""
    assert canonical_url("https://example.test:not-a-port/jobs/123") == ""
    assert canonical_url("ftp://example.test/jobs/123") == ""
    assert canonical_url("https:///jobs/123") == ""
    # A hostname does not need a public suffix for tests, local feeds, or
    # development mirrors to retain its stable posting identity.
    assert canonical_url("https://jobs/123") == "https://jobs/123"
    assert canonical_url("https://localhost:8443/jobs/123") == "https://localhost:8443/jobs/123"


def test_provider_application_variants_share_posting_key_without_merging_ids():
    lever = "https://jobs.lever.co/acme/role-123"
    ashby = "https://jobs.ashbyhq.com/acme/role-123"
    workday = "https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/NY/Engineer_JR123"

    assert posting_key(lever) == posting_key(f"{lever}/apply")
    assert posting_key(ashby) == posting_key(f"{ashby}/application")
    assert posting_key(workday) == posting_key(
        "https://acme.wd5.myworkdayjobs.com/Careers/job/Remote/Engineer_JR123/apply"
    )
    assert posting_key(lever) != posting_key("https://jobs.lever.co/acme/role-124")
    assert posting_key(ashby) != posting_key("https://jobs.ashbyhq.com/acme/role-124")


def test_collapse_jobs_merges_provider_application_suffix_and_keeps_alias():
    jobs = {
        "detail": {
            "id": "detail", "company": "Acme", "title": "SWE",
            "url": "https://jobs.lever.co/acme/role-123",
            "source": "lever", "ats": "lever",
        },
        "apply": {
            "id": "apply", "company": "Acme", "title": "Software Engineer",
            "url": "https://jobs.lever.co/acme/role-123/apply",
            "source": "simplify", "ats": "lever",
        },
    }
    collapsed, aliases, merged = collapse_jobs(jobs)
    assert merged == 1
    assert aliases == {"detail": "apply"} or aliases == {"apply": "detail"}
    survivor = next(iter(collapsed.values()))
    assert {jobs[key]["url"] for key in jobs} - {survivor["url"]} <= set(survivor["alternate_urls"])


def test_merge_keeps_source_board_provenance():
    jobs = {
        "ats": {
            "id": "ats", "company": "Acme", "title": "SWE",
            "url": "https://boards.greenhouse.io/acme/jobs/123",
            "source": "greenhouse", "source_board": "ats:greenhouse:acme",
            "source_board_variants": ["ats:greenhouse:acme"],
        },
        "feed": {
            "id": "feed", "company": "Acme", "title": "SWE",
            "url": "https://job-boards.greenhouse.io/acme/jobs/123/application",
            "source": "simplify", "source_board": "aggregator:simplify",
            "source_board_variants": ["aggregator:simplify"],
        },
    }
    collapsed, _, _ = collapse_jobs(jobs)
    survivor = next(iter(collapsed.values()))
    assert set(survivor["source_board_variants"]) == {
        "ats:greenhouse:acme", "aggregator:simplify",
    }


def test_alias_remap_does_not_overwrite_unrelated_owner_url():
    entries = [{"id": "old", "url": "https://owner.example/handwritten", "stage": "saved"}]
    jobs = {"new": {"id": "new", "url": "https://boards.greenhouse.io/acme/jobs/123"}}

    remap_entry_ids(entries, {"old": "new"}, jobs)

    assert entries[0]["id"] == "new"
    assert entries[0]["url"] == "https://owner.example/handwritten"
    assert "alternate_urls" not in entries[0]


def test_alias_remap_promotes_supported_aggregator_url_and_keeps_original():
    direct = "https://boards.greenhouse.io/acme/jobs/123"
    source = "https://jobright.ai/jobs/info/123"
    entries = [{"id": "old", "url": source, "stage": "interview"}]
    jobs = {"new": {"id": "new", "url": direct}}

    remap_entry_ids(entries, {"old": "new"}, jobs)

    assert entries[0]["id"] == "new"
    assert entries[0]["url"] == direct
    assert source in entries[0]["alternate_urls"]
    assert entries[0]["stage"] == "interview"


def test_alias_traversal_stops_at_surviving_record():
    aliases = {"old": "current", "current": "other"}
    assert resolve_alias("old", aliases, existing={"current": {}, "other": {}}) == "current"


def test_workspace_alias_merge_preserves_owner_state_and_view_lists():
    web = {
        "maybe": ["old"], "excluded": ["old"],
        "jobs": {
            "old": {"notes": "Older owner note", "links": ["first"], "jd": "older JD"},
            "new": {"notes": "Canonical owner note", "links": ["second"], "jd": "current JD"},
        },
    }

    remap_web_jobs(web, {"old": "new"})

    assert web["maybe"] == web["excluded"] == ["new"]
    assert "Older owner note" in web["jobs"]["new"]["notes"]
    assert "Canonical owner note" in web["jobs"]["new"]["notes"]
    assert set(web["jobs"]["new"]["links"]) == {"first", "second"}
    assert web["jobs"]["new"]["jd"] == "current JD"
    assert "older JD" in str(web["jobs"]["new"])


def test_collapse_jobs_prefers_direct_ats_and_returns_alias():
    jobs = {
        "aggregator-id": {
            "id": "aggregator-id", "company": "Acme", "title": "SWE",
            "url": "https://jobs.acme.test/role/?utm_source=feed",
            "source": "simplify", "score": 70, "first_seen": 1,
        },
        "ats-id": {
            "id": "ats-id", "company": "Acme", "title": "Software Engineer",
            "url": "https://jobs.acme.test/role", "source": "greenhouse",
            "ats": "greenhouse", "score": 80, "first_seen": 2,
        },
    }
    collapsed, aliases, merged = collapse_jobs(jobs)
    assert merged == 1
    assert set(collapsed) == {"ats-id"}
    assert aliases == {"aggregator-id": "ats-id"}
    assert "simplify" in collapsed["ats-id"]["source_variants"]


def test_discovered_feed_dedupe_keeps_one_best_provenance():
    aggregator = Job(company="Acme", title="SWE", url="https://acme.test/role",
                     source="simplify")
    ats = Job(company="Acme", title="Software Engineer", url="https://acme.test/role/",
              source="greenhouse", ats="greenhouse", description="full posting")
    selected, dropped = _unique_discovered_jobs([aggregator, ats])
    assert dropped == 1
    assert len(selected) == 1
    assert selected[0].source == "greenhouse"


def test_discovery_retains_distinct_requisitions_with_identical_labels():
    def discover(reverse=False):
        jobs = [
            Job(company="Acme", title="Software Engineer", source="greenhouse",
                locations=["New York, NY"],
                url=f"https://boards.greenhouse.io/acme/jobs/{rid}")
            for rid in (123, 124)
        ]
        selected, dropped = _unique_discovered_jobs(
            list(reversed(jobs)) if reverse else jobs)
        assert len(selected) == 2
        assert dropped == 0
        assert len({getattr(job, "_radar_id", job.id) for job in selected}) == 2
        return {
            job.url: getattr(job, "_radar_id", job.id)
            for job in selected
        }

    assert discover() == discover(True)


def test_existing_legacy_id_collision_gets_url_fallback_without_rewriting_old_key():
    old = Job(company="Acme", title="Software Engineer", source="greenhouse",
              locations=["New York, NY"],
              url="https://boards.greenhouse.io/acme/jobs/123")
    new = Job(company="Acme", title="Software Engineer", source="greenhouse",
              locations=["New York, NY"],
              url="https://boards.greenhouse.io/acme/jobs/124")
    jobs_state = {old.id: old.to_record()}
    posting_ids = _existing_posting_ids(jobs_state)

    new_id = _state_job_identity(new, jobs_state, posting_ids)

    assert new_id != old.id
    assert new_id.startswith(f"{old.id}-")
    assert jobs_state.keys() == {old.id}


def test_discovered_family_dedupe_preserves_audit_metadata_on_fresh_job():
    official = Job(
        company="Google", title="Software Engineer - Campus",
        url="https://www.google.com/about/careers/applications/jobs/results/123",
        source="simplify", locations=["Mountain View, CA"],
    )
    variant = Job(
        company="Google", title="Software Engineer, Early Career, Campus",
        url="https://jobright.ai/jobs/info/mountain-view", source="jobright",
        source_url="https://github.com/jobright-ai/board",
        locations=["Mountain View, CA"],
    )
    variant.link_resolution = {"status": "resolved", "resolved_url": official.url}
    selected, dropped = _unique_discovered_jobs([official, variant])
    assert dropped == 1
    assert len(selected) == 1
    assert selected[0].posting_family_id == selected[0].id
    assert selected[0].posting_identity["variant_count"] == 2


def test_location_family_merges_google_style_official_and_jobright_rows():
    jobs = {
        "google-official": {
            "id": "google-official", "company": "Google",
            "title": "Software Engineer - Campus",
            "url": "https://www.google.com/about/careers/applications/jobs/results/123",
            "source": "simplify", "locations": ["Mountain View, CA", "Cambridge, MA"],
            "last_seen_at": 20,
        },
        "jobright-mv": {
            "id": "jobright-mv", "company": "Google",
            "title": "Software Engineer, Early Career, Campus",
            "url": "https://jobright.ai/jobs/info/mountain-view",
            "source": "jobright", "source_url": "https://github.com/jobright-ai/board",
            "locations": ["Mountain View, CA, United States"], "last_seen_at": 10,
            "link_resolution": {"status": "resolved", "resolved_url":
                                 "https://www.google.com/about/careers/applications/jobs/results/123"},
        },
        "jobright-cambridge": {
            "id": "jobright-cambridge", "company": "Google",
            "title": "Software Engineer, Early Career, Campus",
            "url": "https://jobright.ai/jobs/info/cambridge",
            "source": "jobright", "source_url": "https://github.com/jobright-ai/board",
            "locations": ["Cambridge, MA, United States"], "last_seen_at": 11,
            "link_resolution": {"status": "resolved", "resolved_url":
                                 "https://www.google.com/about/careers/applications/jobs/results/123"},
        },
    }
    collapsed, aliases, merged = collapse_cross_source_jobs(jobs)
    assert merged == 2
    assert set(collapsed) == {"google-official"}
    assert aliases == {"jobright-mv": "google-official", "jobright-cambridge": "google-official"}
    assert collapsed["google-official"]["posting_family_id"] == "google-official"
    assert collapsed["google-official"]["posting_identity"]["variant_count"] == 3
    assert set(collapsed["google-official"]["locations"]) == {
        "Mountain View, CA", "Cambridge, MA", "Mountain View, CA, United States",
        "Cambridge, MA, United States",
    }


def test_same_board_marked_location_variants_collapse_without_an_official_row():
    jobs = {
        "mv": {
            "id": "mv", "company": "Acme", "title": "Software Engineer, New Grad",
            "url": "https://jobright.ai/jobs/info/mv", "source": "jobright",
            "source_url": "https://github.com/jobright-ai/board",
            "locations": ["Mountain View, CA"], "posted_at": 172800,
            "link_resolution": {"status": "resolved", "resolved_url":
                                 "https://boards.greenhouse.io/acme/jobs/123"},
        },
        "ny": {
            "id": "ny", "company": "Acme", "title": "Software Engineer, New Grad",
            "url": "https://jobright.ai/jobs/info/ny", "source": "jobright",
            "source_url": "https://github.com/jobright-ai/board", "locations": ["New York, NY"], "posted_at": 172800,
            "link_resolution": {"status": "resolved", "resolved_url":
                                 "https://boards.greenhouse.io/acme/jobs/123"},
        },
    }
    collapsed, aliases, merged = collapse_location_variants(jobs)
    assert merged == 1
    assert len(collapsed) == 1
    winner = next(iter(collapsed.values()))
    assert aliases == {"ny": winner["id"]} or aliases == {"mv": winner["id"]}
    assert winner["posting_identity"]["variant_count"] == 2


def test_same_board_marked_rows_on_different_days_remain_separate_requisitions():
    jobs = {
        "day-one": {
            "id": "day-one", "company": "Acme", "title": "Software Engineer, New Grad",
            "url": "https://jobright.ai/jobs/info/day-one", "source": "jobright",
            "source_url": "https://github.com/jobright-ai/board",
            "locations": ["Mountain View, CA"], "posted_at": 172800,
        },
        "day-two": {
            "id": "day-two", "company": "Acme", "title": "Software Engineer, New Grad",
            "url": "https://jobright.ai/jobs/info/day-two", "source": "jobright",
            "source_url": "https://github.com/jobright-ai/board",
            "locations": ["New York, NY"], "posted_at": 172800 + 86400,
        },
    }
    collapsed, aliases, merged = collapse_location_variants(jobs)
    assert merged == 0
    assert aliases == {}
    assert set(collapsed) == {"day-one", "day-two"}


def test_same_title_direct_requisitions_remain_separate_when_ambiguous():
    jobs = {
        "one": {
            "id": "one", "company": "Acme", "title": "Software Engineer",
            "url": "https://jobs.acme.test/one", "source": "greenhouse", "ats": "greenhouse",
            "locations": ["New York, NY"],
        },
        "two": {
            "id": "two", "company": "Acme", "title": "Software Engineer",
            "url": "https://jobs.acme.test/two", "source": "greenhouse", "ats": "greenhouse",
            "locations": ["New York, NY"],
        },
        "feed": {
            "id": "feed", "company": "Acme", "title": "Software Engineer",
            "url": "https://jobright.ai/jobs/info/feed", "source": "jobright",
            "locations": ["New York, NY"],
        },
    }
    collapsed, aliases, merged = collapse_cross_source_jobs(jobs)
    assert merged == 0
    assert aliases == {}
    assert set(collapsed) == {"one", "two", "feed"}


def test_matching_titles_without_verified_destination_are_not_identity():
    jobs = {
        "direct": {
            "id": "direct", "company": "Acme", "title": "Software Engineer",
            "url": "https://boards.greenhouse.io/acme/jobs/123",
            "source": "greenhouse", "locations": ["New York, NY"],
        },
        "copy": {
            "id": "copy", "company": "Acme", "title": "Software Engineer",
            "url": "https://jobright.ai/jobs/info/123", "source": "jobright",
            "locations": ["New York, NY"],
        },
    }
    collapsed, aliases, merged = collapse_cross_source_jobs(jobs)
    assert merged == 0
    assert aliases == {}
    assert set(collapsed) == {"direct", "copy"}


def test_latest_verified_closure_wins_and_history_survives_exact_merge():
    direct = "https://boards.greenhouse.io/acme/jobs/123"
    jobs = {
        "open": {
            "id": "open", "company": "Acme", "title": "SWE",
            "url": direct, "source": "greenhouse", "posting_status": "open",
            "lifecycle_checked_at": 10, "alert_ok": True,
        },
        "closed": {
            "id": "closed", "company": "Acme", "title": "SWE",
            "url": direct, "source": "greenhouse", "posting_status": "filled",
            "lifecycle_checked_at": 20, "closed_at": 20,
            "lifecycle_events": [{"status": "filled", "at": 20,
                                  "reason": "Employer filled role"}],
        },
    }
    collapsed, _, _ = collapse_jobs(jobs)
    survivor = next(iter(collapsed.values()))
    assert survivor["posting_status"] == "filled"
    assert survivor["closed_at"] == 20
    assert not survivor["alert_ok"]
    assert any(event["at"] == 20 for event in survivor["lifecycle_events"])


def test_unknown_open_sighting_cannot_outvote_verified_official_closure():
    direct = "https://boards.greenhouse.io/acme/jobs/123"
    jobs = {
        "closed": {
            "id": "closed", "company": "Acme", "title": "SWE",
            "url": direct, "source": "greenhouse", "posting_status": "filled",
            "closed_at": 20, "lifecycle_checked_at": 20,
        },
        "seen": {
            "id": "seen", "company": "Acme", "title": "SWE",
            "url": direct, "source": "simplify", "posting_status": "open",
            "last_seen_at": 100, "lifecycle_checked_at": 100,
        },
    }
    collapsed, _, _ = collapse_jobs(jobs)
    assert next(iter(collapsed.values()))["posting_status"] == "filled"
    jobs["seen"]["last_verified_open_at"] = 100
    collapsed, _, _ = collapse_jobs(jobs)
    assert next(iter(collapsed.values()))["posting_status"] == "open"
    jobs["seen"].pop("last_verified_open_at")
    jobs["seen"]["quality"] = {"live": True, "checked_at": 100}
    collapsed, _, _ = collapse_jobs(jobs)
    assert next(iter(collapsed.values()))["posting_status"] == "open"


def test_terminal_merge_cannot_reenable_alert_from_open_duplicate():
    direct = "https://boards.greenhouse.io/acme/jobs/123"
    jobs = {
        "closed": {
            "id": "closed", "company": "Acme", "title": "SWE",
            "url": direct, "source": "greenhouse", "posting_status": "filled",
            "closed_at": 20, "lifecycle_checked_at": 20, "alert_ok": False,
        },
        "open": {
            "id": "open", "company": "Acme", "title": "SWE",
            "url": direct, "source": "simplify", "posting_status": "open",
            "last_seen_at": 100, "alert_ok": True,
        },
    }
    collapsed, _, _ = collapse_jobs(jobs)
    assert next(iter(collapsed.values()))["alert_ok"] is False


def test_negative_alert_eligibility_survives_exact_duplicate_merge():
    direct = "https://boards.greenhouse.io/acme/jobs/123"
    jobs = {
        "winner": {
            "id": "winner", "company": "Acme", "title": "SWE",
            "url": direct, "source": "greenhouse", "score": 90,
            "alert_ok": False,
        },
        "loser": {
            "id": "loser", "company": "Acme", "title": "SWE",
            "url": direct, "source": "greenhouse", "score": 80,
            "alert_ok": True,
        },
    }
    collapsed, _, _ = collapse_jobs(jobs)
    assert next(iter(collapsed.values()))["alert_ok"] is False


def test_verified_negative_quality_beats_unverified_direct_open_copy():
    direct = "https://boards.greenhouse.io/acme/jobs/123"
    jobs = {
        "stale-open": {
            "id": "stale-open", "company": "Acme", "title": "SWE",
            "url": direct, "source": "greenhouse", "posting_status": "open",
            "lifecycle_checked_at": 10, "last_seen_at": 100,
        },
        "verified-closed": {
            "id": "verified-closed", "company": "Acme", "title": "SWE",
            "url": "https://jobright.ai/jobs/info/123", "source": "simplify",
            "posting_status": "expired",
            "link_resolution": {"status": "resolved", "resolved_url": direct},
            "quality": {"live": False, "dead_status": "expired", "checked_at": 200},
            "lifecycle_checked_at": 200, "closed_at": 200, "alert_ok": False,
        },
    }
    collapsed, _, _ = collapse_jobs(jobs)
    survivor = next(iter(collapsed.values()))
    assert survivor["posting_status"] == "expired"
    assert survivor["quality"]["live"] is False
    assert survivor["alert_ok"] is False


def test_aggregator_closure_does_not_close_live_official_row():
    direct = "https://boards.greenhouse.io/acme/jobs/123"
    jobs = {
        "official": {
            "id": "official", "company": "Acme", "title": "SWE",
            "source": "greenhouse", "url": direct,
            "posting_status": "open", "lifecycle_checked_at": 100,
        },
        "copy": {
            "id": "copy", "company": "Acme", "title": "Software Engineer",
            "source": "jobright", "url": "https://jobright.ai/jobs/info/123",
            "posting_status": "expired", "closed_at": 10,
            "link_resolution": {
                "status": "resolved", "resolved_url": direct,
                "source_status": "closed",
            },
            "lifecycle_events": [{"status": "expired", "at": 10,
                                  "reason": "Jobright closed copy"}],
        },
    }
    collapsed, _, _ = collapse_jobs(jobs)
    survivor = next(iter(collapsed.values()))
    assert survivor["posting_status"] == "open"
    assert "closed_at" not in survivor
    assert any(event["at"] == 10 for event in survivor["lifecycle_events"])
