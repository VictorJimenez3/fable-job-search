from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from radar import main, social
from radar.models import Job
from radar.score import gates


def _companion_module():
    path = Path(__file__).parents[1] / "scripts" / "mac-companion" / "social.py"
    spec = importlib.util.spec_from_file_location("jobradar_social_companion", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _account(**overrides):
    value = {
        "username": "zero2sudo",
        "label": "Zero2Sudo",
        "enabled": True,
        "source_url": "https://www.instagram.com/zero2sudo/",
    }
    value.update(overrides)
    return value


def test_parse_opencli_posts_normalizes_permalink_and_dedupes_shortcode():
    payload = """
posts:
  - shortcode: ABC123
    permalink: https://www.instagram.com/p/ABC123/?utm_source=share
    caption: |
      Acme — Software Engineering Intern
      Apply: https://boards.greenhouse.io/acme/jobs/123
    taken_at: 2026-11-03T12:00:00Z
    owner: {username: zero2sudo}
  - url: https://instagram.com/p/ABC123/
    text: duplicate copy
    username: zero2sudo
  - shortcode: PRIVATE1
    permalink: https://www.instagram.com/p/PRIVATE1/
    caption: unrelated post
    username: someone_else
"""

    posts = social.parse_opencli_posts(payload, _account())

    assert [post["post_id"] for post in posts] == ["ABC123"]
    assert posts[0]["permalink"] == "https://www.instagram.com/p/ABC123/"
    assert posts[0]["caption"].startswith("Acme")
    assert posts[0]["posted_at"] is not None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://www.instagram.com/zero2sudo/p/CxYpVevS2Nx/", "https://www.instagram.com/p/CxYpVevS2Nx/"),
        ("https://www.instagram.com/zero2sudo/reel/C-tHS6ryzjY/", "https://www.instagram.com/reel/C-tHS6ryzjY/"),
    ],
)
def test_parse_opencli_posts_handles_profile_prefixed_post_paths(raw: str, expected: str):
    posts = social.parse_opencli_posts(
        [{"permalink": raw, "caption": "public post", "username": "zero2sudo"}],
        _account(),
    )

    assert posts[0]["permalink"] == expected
    assert posts[0]["post_id"] in expected


def test_social_parser_rejects_external_post_anchor_fixture():
    posts = social.parse_opencli_posts(
        [{
            "permalink": "https://evil.example/p/FAKE123/",
            "caption": "not Instagram",
            "username": "zero2sudo",
        }],
        _account(),
    )

    assert posts == []


def test_social_posts_to_jobs_requires_role_evidence_and_keeps_social_proof():
    posts = [
        {
            "post_id": "GOOD1",
            "username": "zero2sudo",
            "permalink": "https://www.instagram.com/p/GOOD1/",
            "caption": (
                "Acme — Software Engineering Intern\n"
                "Apply: https://boards.greenhouse.io/acme/jobs/123"
            ),
            "posted_at": 1_793_721_600,
            "fetched_at": 1_793_721_700,
        },
        {
            "post_id": "BAD1",
            "username": "zero2sudo",
            "permalink": "https://www.instagram.com/p/BAD1/",
            "caption": "Follow our team for updates https://example.com/careers",
            "posted_at": None,
            "fetched_at": 1_793_721_700,
        },
    ]

    jobs = social.social_posts_to_jobs(posts, _account())

    assert len(jobs) == 1
    job = jobs[0]
    assert job.company == "Acme"
    assert "Intern" in job.title
    assert job.url == "https://boards.greenhouse.io/acme/jobs/123"
    assert job.source == "instagram"
    assert job.source_url == "https://www.instagram.com/p/GOOD1/"
    assert job.posted_at is None
    assert job.alternate_urls == ["https://www.instagram.com/p/GOOD1/"]
    assert job.link_resolution["status"] == "source_only"
    assert job.link_resolution["social_post_id"] == "GOOD1"
    assert job.link_resolution["social_permalink"].endswith("GOOD1/")
    assert job.link_resolution["social_posted_at"] == 1_793_721_600
    assert "Acme" in job.link_resolution["source_note"]
    assert job.description == ""


def test_social_post_rejects_linkedin_and_generic_external_links():
    posts = [{
        "post_id": "NOPE",
        "username": "zero2sudo",
        "permalink": "https://www.instagram.com/p/NOPE/",
        "caption": (
            "Software Engineering Intern at Acme\n"
            "https://www.linkedin.com/jobs/view/12\n"
            "https://example.com/about"
        ),
    }]

    assert social.social_posts_to_jobs(posts, _account()) == []


def test_social_post_with_multiple_job_links_stays_in_review_ledger_only():
    posts = [{
        "post_id": "ROUNDUP",
        "username": "zero2sudo",
        "permalink": "https://www.instagram.com/p/ROUNDUP/",
        "caption": (
            "Acme — Software Engineering Intern\n"
            "https://boards.greenhouse.io/acme/jobs/123\n"
            "https://boards.greenhouse.io/acme/jobs/456"
        ),
    }]

    assert social.social_posts_to_jobs(posts, _account()) == []


@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@boards.greenhouse.io/acme/jobs/123",
        "https://boards.greenhouse.io/acme/jobs/123?access_token=secret",
        "https://boards.greenhouse.io/acme/jobs/123?email=victor@example.com",
    ],
)
def test_social_job_url_rejects_credentials_and_sensitive_query_values(url: str):
    assert social._job_url(url) == ""


def test_social_source_note_redacts_unsafe_urls_and_emails():
    posts = [{
        "post_id": "REDACT",
        "username": "zero2sudo",
        "permalink": "https://www.instagram.com/p/REDACT/",
        "caption": (
            "Acme — Software Engineering Intern victor@example.com\n"
            "Apply: https://boards.greenhouse.io/acme/jobs/123\n"
            "Backup: https://user:password@evil.example/careers?token=secret"
        ),
    }]

    jobs = social.social_posts_to_jobs(posts, _account())

    assert len(jobs) == 1
    note = jobs[0].link_resolution["source_note"]
    assert "victor@example.com" not in note
    assert "password" not in note
    assert "token=secret" not in note
    assert "https://boards.greenhouse.io/acme/jobs/123" in note


def test_collect_social_leads_preserves_cursor_on_auth_failure_and_sets_cooldown(
    tmp_path: Path,
):
    config = tmp_path / "accounts.yaml"
    config.write_text(
        "version: 1\n"
        "defaults: {max_posts: 12, timeout_seconds: 5, failure_cooldown_seconds: 3600}\n"
        "accounts:\n"
        "  - username: zero2sudo\n"
        "    label: Zero2Sudo\n"
        "    enabled: true\n",
        encoding="utf-8",
    )
    state_path = tmp_path / "social.json"
    state_path.write_text(json.dumps({
        "schema_version": 1,
        "accounts": {
            "zero2sudo": {
                "cursor": {"post_id": "OLD", "posted_at": 100},
                "last_success_at": 100,
            }
        },
        "posts": {},
    }), encoding="utf-8")

    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command, 1, stdout="", stderr="login required: Instagram",)

    result = social.collect_social_leads(
        config_path=config,
        state_path=state_path,
        now=1_793_721_700,
        runner=runner,
    )
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    account = saved["accounts"]["zero2sudo"]

    assert len(calls) == 1
    assert result["stats"]["unauthenticated"] == 1
    assert account["status"] == "unauthenticated"
    assert account["cursor"] == {"post_id": "OLD", "posted_at": 100}
    assert account["retry_after"] > 1_793_721_700
    assert saved["posts"] == {}


def test_collect_social_leads_cooldown_skips_repeated_failure(tmp_path: Path):
    config = tmp_path / "accounts.yaml"
    config.write_text(
        "version: 1\naccounts:\n  - {username: zero2sudo, enabled: true}\n",
        encoding="utf-8",
    )
    state_path = tmp_path / "social.json"
    state_path.write_text(json.dumps({
        "schema_version": 1,
        "accounts": {"zero2sudo": {"status": "rate_limited", "retry_after": 200}},
        "posts": {},
    }), encoding="utf-8")

    def runner(*args, **kwargs):
        raise AssertionError("cooldown should avoid invoking OpenCLI")

    result = social.collect_social_leads(
        config_path=config, state_path=state_path, now=100, runner=runner,
    )

    assert result["stats"]["cooldown"] == 1
    assert result["stats"]["attempted"] == 0


@pytest.mark.parametrize(
    ("stderr", "status"),
    [
        ("HTTP 429 Too Many Requests", "rate_limited"),
        ("OpenCLI extension disconnected from Chrome", "unavailable"),
    ],
)
def test_collect_social_leads_classifies_operational_failures_without_retry_loop(
    tmp_path: Path, stderr: str, status: str,
):
    config = tmp_path / "accounts.yaml"
    config.write_text(
        "version: 1\naccounts:\n  - {username: zero2sudo, enabled: true}\n",
        encoding="utf-8",
    )
    state_path = tmp_path / "social.json"
    calls = 0

    def runner(command, **kwargs):
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(command, 1, stdout="", stderr=stderr)

    result = social.collect_social_leads(
        config_path=config, state_path=state_path, now=100, runner=runner,
    )

    assert calls == 1
    assert result["stats"][status] == 1
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["accounts"]["zero2sudo"]["status"] == status
    assert saved["accounts"]["zero2sudo"]["retry_after"] > 100


def test_social_posts_to_jobs_zero_bound_does_not_emit_a_lead():
    assert social.social_posts_to_jobs([
        {
            "post_id": "GOOD1",
            "username": "zero2sudo",
            "permalink": "https://www.instagram.com/p/GOOD1/",
            "caption": "Acme — Software Engineering Intern https://jobs.example.com/careers/123",
        }
    ], _account(), max_jobs=0) == []


def test_instagram_fetcher_is_registered_for_both_job_lanes():
    assert main.AGG_SOURCES["instagram"] is social.fetch_social_leads
    assert main.AGG_SOURCES_INTERNSHIP["instagram"] is social.fetch_social_leads


def test_social_caption_and_stored_posting_hints_cannot_self_promote_source_only_lead():
    job = Job(
        company="Acme",
        title="Software Engineer, New Grad",
        url="https://boards.greenhouse.io/acme/jobs/123",
        source="instagram",
        source_board="public:instagram:zero2sudo",
        description="",
        link_resolution={
            "status": "source_only",
            "source_note": "Acme — Software Engineer, New Grad",
        },
        posting={
            "ranking_evidence": {
                "technical_depth": "Build machine learning services.",
                "ownership": "Own production systems.",
            }
        },
    )

    keep, alert_ok, reasons = gates(job)

    assert keep is True
    assert alert_ok is False
    assert any("review" in reason or "dashboard" in reason for reason in reasons)


def test_social_ledger_prunes_old_and_over_capacity_posts(tmp_path: Path):
    config = tmp_path / "accounts.yaml"
    config.write_text(
        "version: 1\n"
        "defaults: {max_stored_posts: 1, retention_days: 90}\n"
        "accounts:\n  - {username: zero2sudo, enabled: true}\n",
        encoding="utf-8",
    )
    state_path = tmp_path / "social.json"
    state_path.write_text(json.dumps({
        "schema_version": 1,
        "accounts": {"zero2sudo": {"status": "ok"}},
        "posts": {
            "old": {"post_id": "old", "username": "zero2sudo", "fetched_at": 1},
            "recent": {"post_id": "recent", "username": "zero2sudo", "fetched_at": 99},
        },
    }), encoding="utf-8")

    def runner(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="posts: []\n", stderr="")

    social.collect_social_leads(
        config_path=config, state_path=state_path, now=100,
        runner=runner,
    )
    saved = json.loads(state_path.read_text(encoding="utf-8"))

    assert set(saved["posts"]) == {"recent"}


def test_collect_social_leads_treats_empty_success_as_live_and_advances_seen_state(
    tmp_path: Path,
):
    config = tmp_path / "accounts.yaml"
    config.write_text(
        "version: 1\naccounts:\n  - {username: zero2sudo, enabled: true}\n",
        encoding="utf-8",
    )
    state_path = tmp_path / "social.json"

    def runner(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="posts: []\n", stderr="")

    result = social.collect_social_leads(
        config_path=config, state_path=state_path, now=100, runner=runner,
    )
    saved = json.loads(state_path.read_text(encoding="utf-8"))

    assert result["stats"] == {
        "accounts": 1,
        "attempted": 1,
        "success": 1,
        "posts": 0,
        "new_posts": 0,
        "cooldown": 0,
        "unauthenticated": 0,
        "rate_limited": 0,
        "unavailable": 0,
        "errors": 0,
        "empty": 1,
        "deferred": 0,
    }
    assert saved["accounts"]["zero2sudo"]["status"] == "empty"
    assert saved["accounts"]["zero2sudo"]["last_success_at"] == 100
    assert saved["source_status"] == "empty"


def test_collect_social_leads_recovers_direct_links_from_opencli_browser_when_adapter_drops_ids(
    tmp_path: Path,
):
    config = tmp_path / "accounts.yaml"
    config.write_text(
        "version: 1\naccounts:\n  - {username: zero2sudo, enabled: true}\n",
        encoding="utf-8",
    )
    state_path = tmp_path / "social.json"
    responses = iter([
        subprocess.CompletedProcess(
            ["opencli"], 0,
            stdout="- caption: Acme — Software Engineering Intern\n  date: '11/03/2026'\n",
            stderr="",
        ),
        subprocess.CompletedProcess(["opencli"], 0, stdout="tab-1\n", stderr=""),
        subprocess.CompletedProcess(
            ["opencli"], 0,
            stdout=json.dumps({
                "ok": True,
                "data": (
                    "public_confirmed: true\n"
                    "public_proof: logged_out_post_metadata\n"
                    "posts:\n"
                    "  - permalink: https://www.instagram.com/zero2sudo/p/ABC123/\n"
                    "    caption: Acme — Software Engineering Intern https://jobs.example.com/careers/123\n"
                ),
            }),
            stderr="",
        ),
    ])

    def runner(command, **kwargs):
        return next(responses)

    result = social.collect_social_leads(
        config_path=config, state_path=state_path, now=100, runner=runner,
    )
    saved = json.loads(state_path.read_text(encoding="utf-8"))

    assert result["stats"]["success"] == 1
    assert result["stats"]["new_posts"] == 1
    assert saved["posts"]["ABC123"]["permalink"].endswith("ABC123/")
    assert saved["posts"]["ABC123"]["posted_at"] is not None
    assert saved["posts"]["ABC123"]["public_visibility_proof"] == "logged_out_post_metadata"
    assert "img" in social._BROWSER_POST_SCRIPT and ".alt" in social._BROWSER_POST_SCRIPT
    assert "credentials: 'omit'" in social._BROWSER_POST_SCRIPT
    assert "logged_out_post_metadata" in social._BROWSER_POST_SCRIPT
    assert "host === 'instagram.com'" in social._BROWSER_POST_SCRIPT


def test_social_ledger_redacts_noncareer_captions_and_bounds_career_excerpt(tmp_path: Path):
    config = tmp_path / "accounts.yaml"
    config.write_text(
        "version: 1\naccounts:\n  - {username: zero2sudo, enabled: true}\n",
        encoding="utf-8",
    )
    state_path = tmp_path / "social.json"
    career_caption = "Acme — Software Engineering Intern https://jobs.example.com/careers/123 " + ("x" * 1800)
    responses = iter([
        subprocess.CompletedProcess(
            ["opencli"], 0,
            stdout="- caption: Adapter career row\n  date: '11/03/2026'\n",
            stderr="",
        ),
        subprocess.CompletedProcess(["opencli"], 0, stdout="tab-1\n", stderr=""),
        subprocess.CompletedProcess(
            ["opencli"], 0,
            stdout=yaml.safe_dump({
                "public_confirmed": True,
                "posts": [
                    {
                        "permalink": "https://www.instagram.com/p/LIFESTYLE/",
                        "caption": "Private dinner and travel photos",
                    },
                    {"permalink": "https://www.instagram.com/p/CAREER/", "caption": career_caption},
                ],
            }),
            stderr="",
        ),
    ])

    def runner(command, **kwargs):
        return next(responses)

    social.collect_social_leads(
        config_path=config, state_path=state_path, now=100, runner=runner,
    )
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert "caption" not in saved["posts"]["LIFESTYLE"]
    assert len(saved["posts"]["CAREER"]["caption"]) <= 1000
    assert "Software Engineering Intern" in saved["posts"]["CAREER"]["caption"]


def test_browser_generic_login_metadata_without_caption_proof_is_rejected(tmp_path: Path):
    account = _account()
    responses = iter([
        subprocess.CompletedProcess(["opencli"], 0, stdout="tab-1\n", stderr=""),
        subprocess.CompletedProcess(
            ["opencli"], 0,
            stdout=json.dumps({
                "ok": True,
                "data": json.dumps({
                    "posts": [{
                        "permalink": "https://www.instagram.com/zero2sudo/p/UNKNOWN/",
                        "caption": "Acme — Software Engineering Intern https://jobs.example.com/careers/123",
                    }],
                }),
            }),
            stderr="",
        ),
    ])

    def runner(command, **kwargs):
        return next(responses)

    with pytest.raises(social.SocialFetchError) as caught:
        social._browser_fallback_posts(account, max_posts=12, timeout=5, runner=runner)
    assert caught.value.status == "unavailable"
    assert "description.length >= 40" in social._BROWSER_POST_SCRIPT
    assert "matchedWords" in social._BROWSER_POST_SCRIPT


def test_fetch_social_leads_reads_only_enabled_allowlisted_accounts(tmp_path: Path):
    state_path = tmp_path / "social.json"
    state_path.write_text(json.dumps({
        "schema_version": 1,
        "accounts": {"zero2sudo": {"status": "ok", "last_success_at": 100}},
        "posts": {
            "GOOD1": {
                "post_id": "GOOD1",
                "username": "zero2sudo",
                "permalink": "https://www.instagram.com/p/GOOD1/",
                "caption": "Acme — Software Engineer Intern https://jobs.example.com/careers/123",
                "fetched_at": 100,
            },
            "DISABLED": {
                "post_id": "DISABLED",
                "username": "private_account",
                "permalink": "https://www.instagram.com/p/DISABLED/",
                "caption": "Acme — Software Engineer Intern https://jobs.example.com/careers/456",
                "fetched_at": 200,
            },
        },
    }), encoding="utf-8")
    config_path = tmp_path / "accounts.yaml"
    config_path.write_text(
        "version: 1\naccounts:\n  - {username: zero2sudo, enabled: true}\n"
        "  - {username: private_account, enabled: false}\n",
        encoding="utf-8",
    )

    jobs = social.fetch_social_leads(
        config_path=config_path, state_path=state_path, max_jobs=10, now=100,
    )

    assert [job.link_resolution["social_post_id"] for job in jobs] == ["GOOD1"]


def test_fetch_social_leads_suppresses_stale_or_disconnected_account_ledger(tmp_path: Path):
    state_path = tmp_path / "social.json"
    state_path.write_text(json.dumps({
        "schema_version": 1,
        "accounts": {"zero2sudo": {"status": "unavailable", "last_success_at": 100}},
        "posts": {
            "GOOD1": {
                "post_id": "GOOD1",
                "username": "zero2sudo",
                "permalink": "https://www.instagram.com/p/GOOD1/",
                "caption": "Acme — Software Engineer Intern https://jobs.example.com/careers/123",
                "fetched_at": 200,
            }
        },
    }), encoding="utf-8")
    config_path = tmp_path / "accounts.yaml"
    config_path.write_text(
        "version: 1\naccounts:\n  - {username: zero2sudo, enabled: true}\n",
        encoding="utf-8",
    )

    assert social.fetch_social_leads(
        config_path=config_path, state_path=state_path, now=200,
    ) == []


def test_social_companion_does_not_publish_while_crawler_has_dirty_state(tmp_path: Path, monkeypatch):
    companion = _companion_module()
    calls = []

    def run(repo, *command, **kwargs):
        calls.append(command)
        if command[:3] == ("git", "status", "--porcelain"):
            return subprocess.CompletedProcess(command, 0, stdout=" M state/jobs.json\n", stderr="")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(companion, "_run", run)
    companion._publish(tmp_path, "main")

    assert calls == [("git", "status", "--porcelain")]


def test_social_companion_fails_closed_when_git_status_fails(tmp_path: Path, monkeypatch):
    companion = _companion_module()
    calls = []

    def run(repo, *command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="git unavailable")

    monkeypatch.setattr(companion, "_run", run)
    companion._publish(tmp_path, "main")

    assert calls == [("git", "status", "--porcelain")]


def test_social_companion_fails_closed_when_staged_diff_cannot_be_read(
    tmp_path: Path, monkeypatch,
):
    companion = _companion_module()
    calls = []

    def run(repo, *command, **kwargs):
        calls.append(command)
        if command[:3] == ("git", "status", "--porcelain"):
            return subprocess.CompletedProcess(command, 0, stdout=" M state/social.json\n", stderr="")
        if command[:3] == ("git", "add", "--"):
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[:4] == ("git", "diff", "--cached", "--quiet"):
            return subprocess.CompletedProcess(command, 2, stdout="", stderr="diff failed")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(companion, "_run", run)
    companion._publish(tmp_path, "main")

    assert calls[-1] == ("git", "diff", "--cached", "--quiet")


def test_social_companion_retries_an_existing_unpushed_social_commit_without_new_diff(
    tmp_path: Path, monkeypatch,
):
    companion = _companion_module()
    calls = []

    def run(repo, *command, **kwargs):
        calls.append(command)
        if command[:3] == ("git", "status", "--porcelain"):
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[:3] == ("git", "add", "--"):
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[:4] == ("git", "diff", "--cached", "--quiet"):
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[:3] == ("git", "diff", "--name-only"):
            return subprocess.CompletedProcess(command, 0, stdout="state/social.json\n", stderr="")
        if command[:3] == ("git", "push", "origin"):
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(companion, "_run", run)
    companion._publish(tmp_path, "main")

    assert ("git", "push", "origin", "main") in calls
