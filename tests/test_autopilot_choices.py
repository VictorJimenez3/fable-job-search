from radar.applied import autopilot_choices


def test_autopilot_choices_are_open_owner_decisions_and_deduplicated():
    applied = [
        {"id": "direct", "stage": "saved", "applied_at": 10, "url": "https://jobs.ashbyhq.com/acme/role"},
        {"id": "aggregator", "stage": "to_tailor", "tailored_at": 20, "url": "https://jobright.ai/jobs/info/123"},
        {"id": "closed", "stage": "saved", "applied_at": 30},
        {"id": "maybe", "stage": "maybe", "applied_at": 40},
    ]
    jobs = {
        "direct": {
            "id": "direct", "company": "Acme", "title": "Engineer",
            "url": "https://jobs.ashbyhq.com/acme/role", "posting_status": "open",
            "posting_family_id": "family-1", "description": "Build reliable systems.", "score": 80,
        },
        "aggregator": {
            "id": "aggregator", "company": "Acme", "title": "Engineer",
            "url": "https://jobright.ai/jobs/info/123", "alternate_urls": ["https://jobs.ashbyhq.com/acme/role"],
            "posting_status": "open", "posting_family_id": "family-1", "score": 82,
        },
        "closed": {"id": "closed", "company": "Closed", "title": "Engineer", "url": "https://example.com/closed", "posting_status": "expired"},
        "maybe": {"id": "maybe", "company": "Maybe", "title": "Engineer", "url": "https://example.com/maybe", "posting_status": "open"},
    }

    choices = autopilot_choices(applied, jobs)

    assert len(choices) == 1
    assert choices[0]["id"] == "aggregator"
    assert choices[0]["stage"] == "to_tailor"
    assert choices[0]["choice_at"] == 20
    assert choices[0]["url"] == "https://jobs.ashbyhq.com/acme/role"


def test_autopilot_choices_exclude_removed_tracker_rows():
    jobs = {"job-1": {"id": "job-1", "company": "Acme", "title": "Engineer", "url": "https://example.com/job", "posting_status": "open"}}
    applied = [{"id": "job-1", "stage": "saved", "tracker_removed_at": 123}]

    assert autopilot_choices(applied, jobs) == []
