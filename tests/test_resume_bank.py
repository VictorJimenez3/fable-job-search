from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_resume_bank_is_owner_only_private_drive_storage():
    api = (ROOT / "webapp" / "api" / "resume-bank.js").read_text()
    tracker = (ROOT / "webapp" / "api" / "_google-tracker.js").read_text()

    assert 'const { OWNER, session }' in api
    assert "Resume Bank is private to the repository owner" in api
    assert "resumeDriveAccess" in api and "resumeDriveAccess" in tracker
    assert "jobRadarResumeBank" in api
    assert "resumeBankArtifact" in api
    assert '"private, no-store"' in api
    assert "Content-Disposition" in api
    assert "CV/" not in api


def test_resume_bank_frontend_keeps_local_engine_as_sync_source():
    html = (ROOT / "webapp" / "index.html").read_text()

    assert "cloudLibrary" in html
    assert "sync local bank" in html
    assert "Every local run and legacy experiment" in html
    assert "PDFs and previews stay behind the owner session" in html
    assert "Start the private Resume Studio engine before syncing the bank" in html
    assert "New matching, generation, and Workshop edits still use the private Mac engine" in html
