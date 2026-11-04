from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from radar import companion


def _repo(tmp_path: Path, *, branch: str = companion.DEFAULT_BRANCH) -> Path:
    repo = tmp_path / "radar"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", branch], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    (repo / "README").write_text("base", encoding="utf-8")
    subprocess.run(["git", "add", "README"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)
    subprocess.run(["git", "push", "-u", "origin", branch], cwd=repo, check=True, capture_output=True)
    return repo


def test_doctor_is_allowlisted_and_reports_unverified_runtime(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setenv("RADAR_BRANCH", companion.DEFAULT_BRANCH)
    value = companion.doctor(repo)
    encoded = json.dumps(value)
    assert value["ready_for_cutover"] is False
    assert value["capabilities"]["browser_auth"] == "unverified"
    assert value["capabilities"]["schedule"] == "unverified"
    assert "README" not in encoded
    assert str(repo) not in encoded
    assert "JOBRADAR" not in encoded


def test_dry_run_never_executes_or_writes(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setenv("RADAR_DIR", str(repo))
    monkeypatch.setenv("JOBRADAR_COMPANION_PRIMARY", "dot")
    before = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    value = companion.run_operation("social", runtime="dot", repo=repo, execute=False)
    assert value["ok"] is True
    assert value["executed"] is False
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip() == before
    assert set(repo.iterdir()) == {repo / ".git", repo / "README"}


def test_execute_requires_primary_runtime_and_explicit_checkout(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.delenv("RADAR_DIR", raising=False)
    monkeypatch.setenv("JOBRADAR_COMPANION_PRIMARY", "dot")
    value = companion.run_operation("social", runtime="dot", repo=repo, execute=True)
    assert value["ok"] is False
    assert "RADAR_DIR" in value["error"]

    monkeypatch.setenv("RADAR_DIR", str(repo))
    monkeypatch.setenv("JOBRADAR_COMPANION_PRIMARY", "mac")
    value = companion.run_operation("social", runtime="dot", repo=repo, execute=True)
    assert value["ok"] is False
    assert "PRIMARY" in value["error"]


@pytest.mark.parametrize("condition", ["dirty", "wrong_branch", "detached", "unpublished", "missing_ref", "nested"])
def test_execute_refuses_unsafe_checkout(tmp_path, monkeypatch, condition):
    repo = _repo(tmp_path)
    monkeypatch.setenv("RADAR_DIR", str(repo))
    monkeypatch.setenv("RADAR_BRANCH", companion.DEFAULT_BRANCH)
    monkeypatch.setenv("JOBRADAR_COMPANION_PRIMARY", "dot")
    if condition == "dirty":
        (repo / "uncommitted").write_text("keep", encoding="utf-8")
    elif condition == "wrong_branch":
        subprocess.run(["git", "switch", "-c", "feature"], cwd=repo, check=True, capture_output=True)
    elif condition == "detached":
        subprocess.run(["git", "checkout", "--detach"], cwd=repo, check=True, capture_output=True)
    elif condition == "unpublished":
        subprocess.run(["git", "commit", "--allow-empty", "-m", "preserve"], cwd=repo, check=True, capture_output=True)
    elif condition == "missing_ref":
        subprocess.run(
            ["git", "update-ref", "-d", f"refs/remotes/origin/{companion.DEFAULT_BRANCH}"], cwd=repo, check=True,
        )
    else:
        repo = repo / ".git"
        monkeypatch.setenv("RADAR_DIR", str(repo))
    monkeypatch.setattr(companion, "_command", lambda *_: pytest.fail("unsafe checkout executed"))
    value = companion.run_operation("enrich", runtime="dot", repo=repo, execute=True)
    assert value["ok"] is False
    assert "clean" in value["error"]


def test_execute_refuses_manual_git_operation(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setenv("RADAR_DIR", str(repo))
    monkeypatch.setenv("RADAR_BRANCH", companion.DEFAULT_BRANCH)
    monkeypatch.setenv("JOBRADAR_COMPANION_PRIMARY", "dot")
    (repo / ".git" / "rebase-merge").mkdir()
    value = companion.run_operation("enrich", runtime="dot", repo=repo, execute=True)
    assert value["ok"] is False
    assert "clean" in value["error"]


def test_execute_propagates_child_exit_without_leaking_output(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setenv("RADAR_DIR", str(repo))
    monkeypatch.setenv("RADAR_BRANCH", companion.DEFAULT_BRANCH)
    monkeypatch.setenv("JOBRADAR_COMPANION_PRIMARY", "dot")
    fake = tmp_path / "fake.py"
    fake.write_text("raise SystemExit(7)\n", encoding="utf-8")
    monkeypatch.setattr(companion, "_command", lambda operation, checkout: [sys.executable, os.fspath(fake)])
    value = companion.run_operation("social", runtime="dot", repo=repo, execute=True)
    assert value == {
        "ok": False,
        "operation": "social",
        "runtime": "dot",
        "executed": True,
        "exit_code": 7,
        "command_completed": False,
        "outcome": "unverified",
    }


def test_doctor_missing_tools_remains_read_only_and_does_not_disclose_configuration(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RADAR_DIR", str(tmp_path / "absent"))
    monkeypatch.setenv("RADAR_BRANCH", "private-branch-marker")
    monkeypatch.setenv("OPENCLI_PROFILE", "private-profile-marker")
    monkeypatch.setenv("GITHUB_TOKEN", "private-token-marker")
    monkeypatch.setattr(companion.shutil, "which", lambda _: None)
    assert companion.main(["doctor", "--json"]) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["checks"]["git"] is False
    assert report["ready_for_cutover"] is False
    assert report["git"]["remote_ref_freshness"] == "unverified"
    assert "private-" not in output
    assert str(tmp_path) not in output
    assert list(tmp_path.iterdir()) == []


def test_busy_checkout_cannot_be_bypassed_by_inherited_lock_marker(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setenv("RADAR_DIR", str(repo))
    monkeypatch.setenv("JOBRADAR_COMPANION_PRIMARY", "dot")
    monkeypatch.setenv("JOBRADAR_REPO_LOCK_HELD", "1")
    monkeypatch.setattr(companion, "_command", lambda *_: pytest.fail("busy checkout executed"))
    lock = companion._with_lock(repo)
    assert lock is not None
    try:
        value = companion.run_operation("social", runtime="dot", repo=repo, execute=True)
        assert value["executed"] is False
        assert value["status"] == "skipped_busy"
    finally:
        lock.close()


def test_git_inspection_timeout_fails_closed_without_raw_error(tmp_path, monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("private-command-marker", 15)
    monkeypatch.setattr(companion.subprocess, "run", timeout)
    report = companion.doctor(tmp_path)
    assert report["checks"]["git"] is False
    assert "private-command-marker" not in json.dumps(report)


def test_no_posix_lock_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("RADAR_DIR", str(tmp_path))
    monkeypatch.setattr(companion, "fcntl", None)
    result = companion.run_operation("social", runtime="dot", execute=True)
    assert result["executed"] is False
    assert result["error"] == "companion lock is unavailable"


def test_cli_propagates_child_exit_code(monkeypatch, capsys):
    monkeypatch.setattr(companion, "run_operation", lambda *a, **k: {"ok": False, "exit_code": 7})
    assert companion.main(["run", "social", "--execute", "--json"]) == 7
    assert json.loads(capsys.readouterr().out)["exit_code"] == 7


def test_plan_is_default_and_conflicting_flags_do_not_execute(monkeypatch, capsys):
    plans = []
    def planned(*args, **kwargs):
        assert kwargs["execute"] is False
        plans.append(args)
        return {"ok": True, "executed": False}
    monkeypatch.setattr(companion, "run_operation", planned)
    assert companion.main(["run", "social", "--json"]) == 0
    assert len(plans) == 1
    assert companion.main(["run", "social", "--execute", "--dry-run", "--json"]) == 1
    assert len(plans) == 1
    capsys.readouterr()


def test_unknown_operation_and_runtime_never_execute(tmp_path):
    assert companion.run_operation("notify", runtime="dot", repo=tmp_path)["ok"] is False
    assert companion.run_operation("social", runtime="unknown", repo=tmp_path)["ok"] is False
    assert list(tmp_path.iterdir()) == []


def test_json_execution_keeps_diagnostics_on_stderr_and_outcome_unverified(tmp_path, monkeypatch, capfd):
    repo = _repo(tmp_path)
    monkeypatch.setenv("RADAR_DIR", str(repo))
    monkeypatch.setenv("RADAR_BRANCH", companion.DEFAULT_BRANCH)
    monkeypatch.setenv("JOBRADAR_COMPANION_PRIMARY", "dot")
    fake = tmp_path / "fake.py"
    fake.write_text("import os\nassert os.environ['JOBRADAR_REPO_LOCK_HELD'] == '1'\nprint('diagnostic')\n")
    monkeypatch.setattr(companion, "_command", lambda *_: [sys.executable, str(fake)])
    capfd.readouterr()
    assert companion.main(["run", "social", "--execute", "--json"]) == 0
    captured = capfd.readouterr()
    value = json.loads(captured.out)
    assert "diagnostic" in captured.err
    assert value["command_completed"] is True
    assert value["outcome"] == "unverified"
    lock = companion._with_lock(repo)
    assert lock is not None
    lock.close()
