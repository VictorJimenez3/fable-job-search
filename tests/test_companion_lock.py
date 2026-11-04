from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "scripts" / "mac-companion" / "with_repo_lock.py"
RUN = ROOT / "scripts" / "mac-companion" / "run.sh"
SOCIAL = ROOT / "scripts" / "mac-companion" / "social.py"


def _locked_child(repo: Path, *, seconds: float = 0.6) -> subprocess.Popen:
    code = (
        "from pathlib import Path; import time; "
        "Path('started').write_text('yes'); time.sleep(%r)" % seconds
    )
    return subprocess.Popen(
        [sys.executable, str(LOCK), "--repo", str(repo), "--",
         sys.executable, "-c", code],
        cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _wait_for(path: Path) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not path.exists():
        time.sleep(0.01)
    assert path.exists()


def test_repo_lock_is_mutually_exclusive_and_releases_after_child(tmp_path: Path):
    repo = tmp_path / "radar"
    repo.mkdir()
    first = _locked_child(repo)
    _wait_for(repo / "started")

    skipped = subprocess.run(
        [sys.executable, str(LOCK), "--repo", str(repo), "--",
         sys.executable, "-c", "from pathlib import Path; Path('skipped').write_text('no')"],
        cwd=repo, capture_output=True, text=True, check=False,
    )
    assert skipped.returncode == 0
    assert "busy" in skipped.stdout
    assert not (repo / "skipped").exists()
    assert first.wait(timeout=3) == 0

    after = subprocess.run(
        [sys.executable, str(LOCK), "--repo", str(repo), "--",
         sys.executable, "-c", "from pathlib import Path; Path('after').write_text('yes')"],
        cwd=repo, capture_output=True, text=True, check=False,
    )
    assert after.returncode == 0
    assert (repo / "after").read_text() == "yes"


def test_repo_lock_does_not_block_a_different_clone(tmp_path: Path):
    first_repo = tmp_path / "one" / "radar"
    second_repo = tmp_path / "two" / "radar"
    first_repo.mkdir(parents=True)
    second_repo.mkdir(parents=True)
    first = _locked_child(first_repo, seconds=0.8)
    _wait_for(first_repo / "started")
    second_code = "from pathlib import Path; Path('second').write_text('yes')"
    second = subprocess.run(
        [sys.executable, str(LOCK), "--repo", str(second_repo), "--",
         sys.executable, "-c", second_code],
        cwd=second_repo, capture_output=True, text=True, check=False,
    )
    assert second.returncode == 0
    assert (second_repo / "second").exists()
    assert first.wait(timeout=3) == 0


def test_run_sh_skips_before_ollama_when_repo_lock_is_busy(tmp_path: Path):
    repo = tmp_path / "radar"
    repo.mkdir()
    script_dir = repo / "scripts" / "mac-companion"
    script_dir.mkdir(parents=True)
    (script_dir / "run.sh").write_bytes(RUN.read_bytes())
    (script_dir / "run.sh").chmod(0o755)
    (script_dir / "with_repo_lock.py").write_bytes(LOCK.read_bytes())
    (script_dir / "with_repo_lock.py").chmod(0o755)
    first = _locked_child(repo, seconds=0.8)
    _wait_for(repo / "started")
    result = subprocess.run(
        ["bash", str(RUN)],
        cwd=repo,
        env={**os.environ, "RADAR_DIR": str(repo), "JOBRADAR_VENV": str(tmp_path / "missing")},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert "repo-lock: busy" in result.stdout
    assert "ollama" not in (result.stdout + result.stderr).lower()
    assert first.wait(timeout=3) == 0


def test_run_sh_preserves_unpublished_social_commit_during_sync(tmp_path: Path):
    remote = tmp_path / "remote.git"
    repo = tmp_path / "radar"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                   capture_output=True, text=True)
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True,
                   capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    (repo / "README").write_text("base")
    script_dir = repo / "scripts" / "mac-companion"
    script_dir.mkdir(parents=True)
    (script_dir / "run.sh").write_bytes(RUN.read_bytes())
    (script_dir / "run.sh").chmod(0o755)
    (script_dir / "with_repo_lock.py").write_bytes(LOCK.read_bytes())
    (script_dir / "with_repo_lock.py").chmod(0o755)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True,
                   capture_output=True, text=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=repo, check=True,
                   capture_output=True, text=True)

    state = repo / "state"
    state.mkdir()
    social = state / "social.json"
    social.write_text('{"posts":{"PRIVATE":"keep"}}\n')
    subprocess.run(["git", "add", str(social)], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "social"], cwd=repo, check=True,
                   capture_output=True, text=True)
    before = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

    result = subprocess.run(
        ["bash", str(repo / "scripts" / "mac-companion" / "run.sh")],
        cwd=repo,
        env={**os.environ, "RADAR_DIR": str(repo), "RADAR_BRANCH": "main",
             "JOBRADAR_SYNC_ONLY": "1", "JOBRADAR_VENV": str(tmp_path / "missing")},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert "unpublished social state" in result.stdout
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip() == before
    assert social.read_text() == '{"posts":{"PRIVATE":"keep"}}\n'


def test_run_sh_preserves_any_clean_unpublished_commit_during_sync(tmp_path: Path):
    remote = tmp_path / "remote.git"
    repo = tmp_path / "radar"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                   capture_output=True, text=True)
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True,
                   capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    script_dir = repo / "scripts" / "mac-companion"
    script_dir.mkdir(parents=True)
    (script_dir / "run.sh").write_bytes(RUN.read_bytes())
    (script_dir / "run.sh").chmod(0o755)
    (script_dir / "with_repo_lock.py").write_bytes(LOCK.read_bytes())
    (script_dir / "with_repo_lock.py").chmod(0o755)
    (repo / "README").write_text("base")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True,
                   capture_output=True, text=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=repo, check=True,
                   capture_output=True, text=True)
    owner_note = repo / "OWNER_NOTE"
    owner_note.write_text("keep")
    subprocess.run(["git", "add", "OWNER_NOTE"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "owner note"], cwd=repo, check=True,
                   capture_output=True, text=True)
    before = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

    result = subprocess.run(
        ["bash", str(repo / "scripts" / "mac-companion" / "run.sh")],
        cwd=repo,
        env={**os.environ, "RADAR_DIR": str(repo), "RADAR_BRANCH": "main",
             "JOBRADAR_SYNC_ONLY": "1", "JOBRADAR_VENV": str(tmp_path / "missing")},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert "local commits are ahead of origin" in result.stdout
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip() == before
    assert owner_note.read_text() == "keep"


def test_social_entrypoint_uses_shared_repo_lock_before_collecting(tmp_path: Path):
    repo = tmp_path / "radar"
    repo.mkdir()
    script_dir = repo / "scripts" / "mac-companion"
    script_dir.mkdir(parents=True)
    social_script = script_dir / "social.py"
    social_script.write_bytes(SOCIAL.read_bytes())
    (script_dir / "with_repo_lock.py").write_bytes(LOCK.read_bytes())
    (script_dir / "with_repo_lock.py").chmod(0o755)
    package = repo / "radar"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "social.py").write_text(
        "from pathlib import Path\n"
        "def collect_social_leads():\n"
        "    Path('collected').write_text('unexpected')\n"
        "    return {'stats': {}}\n"
    )
    first = _locked_child(repo, seconds=0.8)
    _wait_for(repo / "started")
    result = subprocess.run(
        [sys.executable, str(social_script)],
        cwd=repo,
        env={**os.environ, "RADAR_DIR": str(repo)},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert "repo-lock: busy" in result.stdout
    assert not (repo / "collected").exists()
    assert first.wait(timeout=3) == 0
