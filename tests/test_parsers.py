"""Parser tests run against real captured samples of each aggregator format."""
import json
from unittest.mock import patch

from conftest import FIXTURES

from radar.sources import aggregators


def test_simplify_parses_active_only():
    data = json.loads((FIXTURES / "simplify_sample.json").read_text())
    with patch.object(aggregators, "get_json", return_value=data):
        jobs = aggregators.fetch_simplify()
    assert len(jobs) >= 10
    j = jobs[0]
    assert j.company and j.title and j.url.startswith("http")
    assert j.posted_at is not None
    assert all(job.source == "simplify" for job in jobs)
    # inactive rows in the fixture must have been dropped
    n_active_recent = sum(1 for r in data if r.get("active"))
    assert len(jobs) <= n_active_recent


def test_vansh_parses():
    data = json.loads((FIXTURES / "vansh_sample.json").read_text())
    with patch.object(aggregators, "get_json", return_value=data):
        jobs = aggregators.fetch_vansh()
    assert all(j.source == "vansh" for j in jobs)
    assert all(j.company for j in jobs)


def test_jobright_markdown_table():
    md = (FIXTURES / "jobright_sample.md").read_text()
    with patch.object(aggregators, "get_text", return_value=md):
        jobs = aggregators.fetch_jobright()
    # two URLs are fetched; each returns the same fixture
    assert len(jobs) >= 20
    j = jobs[0]
    assert j.url.startswith("http")
    assert j.posted_at is not None
    assert j.company and "|" not in j.company


def test_jobright_continuation_rows_resolve_or_drop():
    md = (FIXTURES / "jobright_sample.md").read_text()
    with patch.object(aggregators, "get_text", return_value=md):
        jobs = aggregators.fetch_jobright()
    assert not any(j.company in aggregators._CONTINUATION_GLYPHS for j in jobs)
    assert any(j.company == "Roblox" and "User Sharing" in j.title for j in jobs)
    # a table that STARTS with a continuation row can't be resolved → dropped
    orphan = ("| Company | Job Title | Location | Work Model | Date Posted |\n"
              "| --- | --- | --- | --- | --- |\n"
              "| ↳ | **[Ghost Role](https://x.test/1)** | NYC | On Site | Jul 05 |\n")
    with patch.object(aggregators, "get_text", return_value=orphan):
        jobs = aggregators.fetch_jobright()
    assert jobs == []


def test_scrub_glyph_companies_removes_corrupt_records():
    from radar.main import scrub_glyph_companies
    jobs = {
        "a": {"company": "↳", "title": "Software Engineer I", "alert_ok": True},
        "b": {"company": "&#8627;", "title": "Analyst"},
        "c": {"company": "Stripe", "title": "Software Engineer"},
    }
    assert scrub_glyph_companies(jobs) == 2
    assert list(jobs) == ["c"]
    assert scrub_glyph_companies(jobs) == 0   # idempotent


def test_speedyapply_markdown_table():
    md = (FIXTURES / "speedy_sample.md").read_text()
    with patch.object(aggregators, "get_text", return_value=md):
        jobs = aggregators.fetch_speedyapply()
    # the fixture contains 15 rows; some are older than the 45-day cutoff
    assert len(jobs) >= 5
    j = jobs[0]
    assert j.url.startswith("http")
    assert j.posted_at is not None
    # speedyapply links point straight at ATS pages — that's the discovery goldmine
    assert any("myworkdayjobs" in x.url or "greenhouse" in x.url or "lever" in x.url
               or "ashby" in x.url or "smartrecruiters" in x.url for x in jobs)
