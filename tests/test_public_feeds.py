from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from radar.http import UnsafeURL
from radar.sources import public_feeds


def test_yubhub_schema_json_maps_explicit_fields_and_direct_apply_url():
    payload = {
        "x-facet": {"type": "graduate", "slug": "engineering"},
        "jobs": [{
            "@type": "JobPosting",
            "title": "Software Engineer",
            "description": "<p>Build data services.</p>",
            "datePosted": "2026-11-02T00:00:00.000Z",
            "hiringOrganization": {"name": "Acme"},
            "jobLocation": {"address": {"addressLocality": "New York, NY"}},
            "url": "https://jobs.example.com/acme/1",
        }],
    }

    jobs = public_feeds.parse_yubhub_payload(payload, source="yubhub_graduate_engineering")

    assert len(jobs) == 1
    assert jobs[0].company == "Acme"
    assert jobs[0].title == "Software Engineer"
    assert jobs[0].url == "https://jobs.example.com/acme/1"
    assert jobs[0].description == "Build data services."
    assert jobs[0].locations == ["New York, NY"]
    assert jobs[0].posted_at is not None


def test_yubhub_parser_skips_malformed_rows_linkedin_and_duplicate_urls_with_bound():
    row = {
        "title": "Data Scientist",
        "hiringOrganization": {"name": "Acme"},
        "url": "https://jobs.example.com/acme/2",
    }
    payload = {
        "jobs": [
            row,
            dict(row, title="Duplicate title", url="https://jobs.example.com/acme/2?utm_source=feed"),
            dict(row, title="Distinct requisition", url="https://jobs.example.com/acme/3"),
            dict(row, url="https://www.linkedin.com/jobs/view/123"),
            {"title": "Missing URL", "hiringOrganization": {"name": "Acme"}},
            {"title": "Missing company", "url": "https://jobs.example.com/acme/3"},
        ]
    }

    jobs = public_feeds.parse_yubhub_payload(payload, max_jobs=10)

    assert [(job.company, job.title, job.url) for job in jobs] == [
        ("Acme", "Data Scientist", "https://jobs.example.com/acme/2"),
        ("Acme", "Distinct requisition", "https://jobs.example.com/acme/3"),
    ]
    assert public_feeds.parse_yubhub_payload(payload, max_jobs=0) == []


def test_yubhub_parser_raises_on_invalid_schema_but_accepts_explicit_empty_jobs():
    with pytest.raises(ValueError, match="jobs list"):
        public_feeds.parse_yubhub_payload({"items": []})
    assert public_feeds.parse_yubhub_payload({"jobs": []}) == []


def test_headcount_markdown_parses_requested_table_without_fabricating_date():
    markdown = (
        "# Entry-Level and New Grad Jobs\n\n"
        "## Software engineering\n\n"
        "| Company | Role | Location | Remote | Pay | Posted | Apply | Details |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
        '| **Verkada** | Backend Software Engineer - University Graduate 2027 | '
        'San Mateo | On-site | $120k–$140k/yr | Nov 2 | '
        '<a href="https://jobs.example/verkada/1">Apply</a> | '
        '<a href="https://headcountlabs.com/catalog/jobs/abc">Details</a> |\n\n'
        "## Data, AI and machine learning\n\n"
        "| Company | Role | Location | Remote | Pay | Posted | Apply | Details |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
        '| **Acme** | Data Scientist - New Grad | Remote | Remote | — | Nov 2 | '
        '<a href="https://jobs.example/data/1">Apply</a> | '
        '<a href="https://headcountlabs.com/catalog/jobs/def">Details</a> |\n'
    )

    jobs = public_feeds.parse_headcount_markdown(markdown, section="Software engineering")

    assert len(jobs) == 1
    assert jobs[0].company == "Verkada"
    assert jobs[0].title == "Backend Software Engineer - University Graduate 2027"
    assert jobs[0].locations == ["San Mateo"]
    assert jobs[0].salary == "$120k–$140k/yr"
    assert jobs[0].posted_at is None  # "Nov 2" has no trustworthy year.
    assert jobs[0].remote is False


def test_headcount_parser_skips_linkedin_and_respects_bound():
    markdown = (
        "## Software engineering\n"
        "| Company | Role | Location | Remote | Pay | Posted | Apply | Details |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
        '| **A** | Engineer One | Remote | Remote | — | Nov 2 | '
        '<a href="https://jobs.example/1">Apply</a> | '
        '<a href="https://headcountlabs.com/catalog/jobs/1">Details</a> |\n'
        '| **B** | Engineer Two | Remote | Remote | — | Nov 2 | '
        '<a href="https://www.linkedin.com/jobs/view/2">Apply</a> | '
        '<a href="https://headcountlabs.com/catalog/jobs/2">Details</a> |\n'
        '| **C** | Engineer Three | Remote | Remote | — | Nov 2 | '
        '<a href="https://jobs.example/3">Apply</a> | '
        '<a href="https://headcountlabs.com/catalog/jobs/3">Details</a> |\n'
    )

    jobs = public_feeds.parse_headcount_markdown(markdown, section="Software engineering", max_jobs=1)

    assert len(jobs) == 1
    assert jobs[0].url == "https://jobs.example/1"


def test_headcount_internship_sections_keep_both_freshness_buckets_and_mark_source_only():
    markdown = (
        "## Posted in the last 7 days\n"
        "| Company | Role | Location | Remote | Pay | Posted | Apply | Details |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
        '| **A** | Summer 2027 Software Engineering Intern | Remote | Remote | — | Nov 2 | '
        '<a href="https://jobs.example/1">Apply</a> | details |\n'
        "## Posted 8 to 30 days ago\n"
        "| Company | Role | Location | Remote | Pay | Posted | Apply | Details |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
        '| **B** | Data Science Intern | Boston | — | — | Oct 20 | '
        '<a href="https://jobs.example/2">Apply</a> | details |\n'
    )
    jobs = public_feeds.parse_headcount_markdown(
        markdown,
        section=["Posted in the last 7 days", "Posted 8 to 30 days ago"],
        source="headcount_data_ai_internships",
        max_jobs=10,
    )
    assert [job.company for job in jobs] == ["A", "B"]
    assert all(job.link_resolution["status"] == "source_only" for job in jobs)


def test_headcount_new_grad_feed_does_not_inherit_internship_review_marker():
    markdown = (
        "## Software engineering\n"
        "| Company | Role | Location | Remote | Pay | Posted | Apply | Details |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
        '| **A** | Software Engineer, New Grad | Remote | Remote | — | Nov 2 | '
        '<a href="https://jobs.example/grad-1">Apply</a> | details |\n'
    )
    jobs = public_feeds.parse_headcount_markdown(markdown, source="headcount_engineering")
    assert jobs and jobs[0].link_resolution == {}


def test_headcount_posted_month_day_uses_explicit_readme_update_year():
    markdown = (
        "**updated November 3, 2026 at 4:33 AM EST**\n"
        "## Posted in the last 7 days\n"
        "| Company | Role | Location | Remote | Pay | Posted | Apply | Details |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
        '| **A** | Summer 2027 Software Engineering Intern | Remote | — | — | Nov 2 | '
        '<a href="https://jobs.example/1">Apply</a> | details |\n'
    )
    jobs = public_feeds.parse_headcount_markdown(
        markdown, section="Posted in the last 7 days", source="headcount_data_ai_internships"
    )
    assert datetime.fromtimestamp(jobs[0].posted_at, tz=UTC).date().isoformat() == "2026-11-02"


def test_headcount_full_posted_date_parses_without_update_anchor():
    markdown = (
        "## Posted in the last 7 days\n"
        "| Company | Role | Location | Remote | Pay | Posted | Apply | Details |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
        '| **A** | Software Engineering Intern | Remote | — | — | Nov 2, 2026 | '
        '<a href="https://jobs.example/1">Apply</a> | details |\n'
    )
    jobs = public_feeds.parse_headcount_markdown(
        markdown, section="Posted in the last 7 days", source="headcount_data_ai_internships"
    )
    assert datetime.fromtimestamp(jobs[0].posted_at, tz=UTC).date().isoformat() == "2026-11-02"


def test_headcount_posted_december_rolls_back_when_readme_updates_in_january():
    markdown = (
        "**updated January 2, 2027 at 4:33 AM EST**\n"
        "## Posted in the last 7 days\n"
        "| Company | Role | Location | Remote | Pay | Posted | Apply | Details |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
        '| **A** | Winter 2027 Engineering Intern | Remote | — | — | Dec 31 | '
        '<a href="https://jobs.example/1">Apply</a> | details |\n'
    )
    jobs = public_feeds.parse_headcount_markdown(
        markdown, section="Posted in the last 7 days", source="headcount_engineering_internships"
    )
    assert datetime.fromtimestamp(jobs[0].posted_at, tz=UTC).date().isoformat() == "2026-12-31"


def test_zero2sudo_notion_parser_returns_source_only_leads_without_using_page_edit_time():
    payload = {
        "recordMap": {
            "block": {
                "row": {"value": {"value": {
                    "id": "row",
                    "type": "table_row",
                    "properties": {
                        "link": [["Job Posting", [["a", "https://jobs.example.com/4"]]]],
                        "title": [["Acme — Software Engineer, New Grad"]],
                        "notes": [["Apply by Nov 30, 2026."]],
                    },
                    "last_edited_time": 9999999999999,
                }}}
            }
        }
    }

    jobs = public_feeds.parse_zero2sudo_notion(payload, max_jobs=5)

    assert len(jobs) == 1
    assert jobs[0].company == "Acme"
    assert jobs[0].title == "Software Engineer, New Grad"
    assert jobs[0].posted_at is None
    assert jobs[0].link_resolution["status"] == "source_only"
    assert "employer" in jobs[0].link_resolution["reason"]
    assert jobs[0].description == ""
    assert jobs[0].link_resolution["source_note"] == "Apply by Nov 30, 2026."
    assert jobs[0].alert_ok is False


def test_zero2sudo_notion_parser_skips_linkedin_and_non_job_rows():
    payload = {
        "recordMap": {"block": {
            "header": {"value": {"value": {
                "type": "sub_header", "properties": {"title": [["IMPORTANT 2026 JOB LINKS"]]}
            }}},
            "linkedin": {"value": {"value": {
                "type": "table_row", "properties": {
                    "link": [["Job Posting", [["a", "https://www.linkedin.com/jobs/view/1"]]]],
                    "title": [["LinkedIn — Apprentice Engineer"]],
                }
            }}},
        }}
    }

    assert public_feeds.parse_zero2sudo_notion(payload) == []


def test_fetchers_propagate_upstream_error_to_aggregator_boundary():
    with patch.object(public_feeds, "get_json", side_effect=UnsafeURL("blocked")):
        with pytest.raises(UnsafeURL):
            public_feeds.fetch_yubhub_graduate_engineering()
    with patch.object(public_feeds, "get_text", side_effect=RuntimeError("upstream")):
        with pytest.raises(RuntimeError, match="upstream"):
            public_feeds.fetch_headcount_engineering()
    with patch.object(public_feeds, "post_json", side_effect=RuntimeError("upstream")):
        with pytest.raises(RuntimeError, match="upstream"):
            public_feeds.fetch_zero2sudo_notion(enabled=True)


def test_yubhub_fetch_attaches_required_attribution_to_source_metadata():
    payload = {
        "jobs": [{
            "title": "Engineer",
            "hiringOrganization": {"name": "Acme"},
            "url": "https://jobs.example.com/acme/6",
        }]
    }
    with patch.object(public_feeds, "get_json", return_value=payload):
        jobs = public_feeds.fetch_yubhub_graduate_engineering()
    assert "Data by YubHub" in jobs[0].source_board


def test_zero2sudo_fetcher_is_enabled_but_returns_review_only_leads():
    payload = {
        "recordMap": {"block": {
            "row": {"value": {"value": {
                "type": "table_row", "properties": {
                    "link": [["Job Posting", [["a", "https://jobs.example.com/5"]]]],
                    "title": [["Acme — New Grad Engineer"]],
                }
            }}}
        }}
    }
    with patch.object(public_feeds, "post_json", return_value=payload) as post:
        jobs = public_feeds.fetch_zero2sudo_notion()
    assert len(jobs) == 1
    assert jobs[0].link_resolution["status"] == "source_only"
    post.assert_called_once()
