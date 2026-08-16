from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_resume_bank_is_owner_only_private_drive_storage():
    api = (ROOT / "webapp" / "api" / "resume-bank.js").read_text()
    tracker = (ROOT / "webapp" / "api" / "_google-tracker.js").read_text()

    assert 'const { OWNER, session, requireMutationRequest }' in api
    assert "requireMutationRequest(req, res)" in api
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


def test_resume_bank_exposes_owner_only_objective_per_posting_comparison():
    html = (ROOT / "webapp" / "index.html").read_text()
    api = (ROOT / "webapp" / "api" / "resume-bank.js").read_text()

    assert "objectiveRankingHTML" in html
    assert "best for this posting" in html
    assert "not a hiring prediction" in html
    assert "transparent match, evidence-safety, layout, and portfolio signals" in html
    assert "entry.objective" in api


def test_resume_bank_exposes_visual_keyword_audit_and_context_followup():
    html = (ROOT / "webapp" / "index.html").read_text()
    api = (ROOT / "webapp" / "api" / "resume-bank.js").read_text()

    assert "ATS keyword map" in html
    assert "Available but omitted" in html
    assert "Diagnostic comparison—not a recruiter ATS score" in html
    assert "reviewKeywordInContext" in html
    assert "Possible places to investigate" in html
    assert "/api/context/hint" in html
    assert "not in this place" in html
    assert "entry.keyword_audit" in api
    assert "keyword_audit:entry.keyword_audit||cloud.keyword_audit" in html
