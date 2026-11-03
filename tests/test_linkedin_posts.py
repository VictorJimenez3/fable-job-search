from __future__ import annotations

from unittest.mock import patch

from radar.sources import linkedin_posts


def test_queries_use_candidate_graduation_year(monkeypatch):
    monkeypatch.setattr(linkedin_posts, "profile", lambda: {"candidate": {"grad_year": 2029}})

    queries = linkedin_posts.build_queries()

    assert any("2029" in query for query in queries)
    assert all("2026" not in query for query in queries)


def test_invalid_candidate_year_falls_back_to_current_year(monkeypatch):
    monkeypatch.setattr(linkedin_posts, "profile", lambda: {"candidate": {"grad_year": "not-a-year"}})
    monkeypatch.setattr(linkedin_posts, "current_year", lambda: 2031)

    assert linkedin_posts.candidate_grad_year() == 2031


def test_fetch_posts_accepts_explicit_queries_and_skips_linkedin_nonposts():
    payloads = [
        {"items": [
            {"title": "Hiring update", "snippet": "New grad SWE", "link": "https://www.linkedin.com/posts/acme-1"},
            {"title": "Company page", "snippet": "Not a post", "link": "https://www.linkedin.com/company/acme"},
        ]},
    ]

    env_values = {"GOOGLE_CSE_KEY": "k", "GOOGLE_CSE_ID": "c"}
    with patch.object(linkedin_posts, "env", side_effect=lambda name: env_values.get(name, "")), \
         patch.object(linkedin_posts, "get_json", side_effect=payloads) as get_json:
        posts = linkedin_posts.fetch_posts(max_per_query=2, queries=["site:linkedin.com/posts test"])

    assert [post["url"] for post in posts] == ["https://www.linkedin.com/posts/acme-1"]
    assert get_json.call_args.kwargs["params"]["q"] == "site:linkedin.com/posts test"


def test_fetch_posts_returns_empty_without_search_credentials():
    with patch.object(linkedin_posts, "env", return_value=""):
        assert linkedin_posts.fetch_posts(queries=["test"]) == []
