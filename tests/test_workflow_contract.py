import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_tracker_sync_is_scheduled_and_serialized_with_application_writes():
    workflow = (ROOT / ".github/workflows/tracker-sync.yml").read_text()
    assert 'cron: "*/15 * * * *"' in workflow
    assert "group: applied-sync" in workflow
    assert "python -m radar.main tracker-sync" in workflow
    assert "NOTION_TOKEN" in workflow


def test_workflow_recovery_retries_once_and_gives_codex_handoff():
    workflow = (ROOT / ".github/workflows/workflow-recovery.yml").read_text()
    assert "workflow_run:" in workflow
    assert "rerun-failed-jobs" in workflow
    assert "run_attempt > 1" in workflow
    assert "Tell Codex: fix workflow run" in workflow
    assert "actions: write" in workflow
    assert "cancelled" in workflow
    assert "github.paginate" in workflow
    assert 'actions/runs/{run_id}/rerun"' in workflow


def test_workflow_recovery_does_not_retry_deterministic_snapshot_size_failures():
    workflow = (ROOT / ".github/workflows/workflow-recovery.yml").read_text()
    assert "downloadJobLogsForWorkflowRun" in workflow
    assert "generated job (?:snapshot|detail shard) is" in workflow
    assert "deterministic state-size failure" in workflow
    assert "no automatic rerun" in workflow


def test_radar_publishes_detail_shards_and_keeps_the_primary_guard():
    workflow = (ROOT / ".github/workflows/radar.yml").read_text()
    assert "jobs_details" in workflow or "detail shard" in workflow
    assert "RADAR_MAX_JOB_DETAIL_SHARD_BYTES" in workflow
    assert "generated job (snapshot|detail shard) is" in workflow


def test_scheduled_link_repair_is_bounded_but_manual_cli_can_override_it():
    workflow = (ROOT / ".github/workflows/radar.yml").read_text()
    assert 'RADAR_LINK_RESOLVE_LIMIT: "100"' in workflow
    assert "python -m radar.main resolve-links" in workflow


def test_radar_push_race_checkpoint_preserves_detail_shards_and_aliases():
    workflow = (ROOT / ".github/workflows/radar.yml").read_text()
    assert "git ls-tree -r --name-only HEAD" in workflow
    assert "state/jobs_details" in workflow
    assert "state/intern_jobs_details" in workflow
    assert 'relative_path="${detail_path#state/}"' in workflow
    assert '"$checkpoint_dir/$relative_path"' in workflow
    assert "MERGE_CRAWL_ALIASES" in workflow


def test_detail_checkpoint_materializes_shards_next_to_jobs_snapshot(tmp_path):
    repo = tmp_path / "repo"
    (repo / "state/jobs_details").mkdir(parents=True)
    (repo / "state/intern_jobs_details").mkdir(parents=True)
    (repo / "state/jobs_details/ab.json").write_text('{"schema_version":1}\n')
    (repo / "state/intern_jobs_details/cd.json").write_text('{"schema_version":1}\n')
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "state"], cwd=repo, check=True)
    subprocess.run([
        "git", "-c", "user.name=test", "-c", "user.email=test@example.test",
        "commit", "-qm", "fixture",
    ], cwd=repo, check=True)
    checkpoint = tmp_path / "checkpoint"
    script = r'''set -eu
for detail_dir in state/jobs_details state/intern_jobs_details; do
  while IFS= read -r detail_path; do
    [ -n "$detail_path" ] || continue
    relative_path="${detail_path#state/}"
    mkdir -p "$CHECKPOINT/${relative_path%/*}"
    git show "HEAD:$detail_path" > "$CHECKPOINT/$relative_path"
  done < <(git ls-tree -r --name-only HEAD -- "$detail_dir")
done
'''
    subprocess.run(["bash", "-c", script], cwd=repo, env={
        "CHECKPOINT": str(checkpoint),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }, check=True)
    assert (checkpoint / "jobs_details/ab.json").exists()
    assert (checkpoint / "intern_jobs_details/cd.json").exists()
    assert not (checkpoint / "state/jobs_details/ab.json").exists()


def test_vercel_deploy_waits_for_green_tests_and_verifies_the_exact_build():
    workflow = (ROOT / ".github/workflows/vercel-production.yml").read_text()
    assert "workflow_run:" in workflow
    assert "github.event.workflow_run.conclusion == 'success'" in workflow
    assert "github.event.workflow_run.head_sha" in workflow
    assert "job-radar-build" in workflow


def test_all_new_grad_state_writers_stage_detail_shards_with_the_index():
    score = (ROOT / ".github/workflows/score-maintenance.yml").read_text()
    sponsorship = (ROOT / ".github/workflows/sponsorship.yml").read_text()
    assert "state/jobs_history.json" in score
    assert "state/jobs_details" in score
    assert "state/jobs_history.json" in sponsorship
    assert "state/jobs_details" in sponsorship


def test_internship_writer_stages_detail_shards_and_restarts_after_push_race():
    workflow = (ROOT / ".github/workflows/internship-radar.yml").read_text()
    assert workflow.count("state/intern_jobs_details") >= 2
    assert "state/intern_*.json" in workflow
    assert "MERGE_CRAWL_JOBS_HISTORY" in workflow
    assert "RADAR_PROFILE=internship" in workflow
    assert "git ls-tree -r --name-only HEAD -- state/intern_jobs_details" in workflow
    assert 'git reset --hard "origin/${GITHUB_REF_NAME}"' in workflow
    assert "git pull --rebase --autostash || true" not in workflow
    crawl_index = workflow.index("python -m radar.main crawl")
    crawl_loop = workflow.index("for i in 1 2 3 4; do")
    assert crawl_index < crawl_loop
    assert workflow.count("python -m radar.main crawl") == 1
    rescore_step = workflow.index("Rescore stored internship state only")
    rescore_loop = workflow.index("for i in 1 2 3; do", rescore_step)
    assert workflow.index("python -m radar.main rescore", rescore_loop) > rescore_loop


def test_crawl_merge_script_resolves_the_internship_state_namespace():
    script = (ROOT / "scripts/mac-companion/merge_crawl_state.py").read_text()
    assert '"intern_" if lane in {"internship", "intern"} else ""' in script
    assert "_state_path(\"jobs.json\")" in script
