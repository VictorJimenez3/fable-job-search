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


def test_vercel_deploy_waits_for_green_tests_and_verifies_the_exact_build():
    workflow = (ROOT / ".github/workflows/vercel-production.yml").read_text()
    assert "workflow_run:" in workflow
    assert "github.event.workflow_run.conclusion == 'success'" in workflow
    assert "github.event.workflow_run.head_sha" in workflow
    assert "job-radar-build" in workflow
