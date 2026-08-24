from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_application_agent_keeps_owner_cloud_and_local_boundaries():
    api = (ROOT / "webapp" / "api" / "_application-agent.js").read_text()
    routing = (ROOT / "webapp" / "vercel.json").read_text()
    local = (ROOT / "radar" / "application_agent.py").read_text()
    service = (ROOT / "scripts" / "resume_studio.py").read_text()
    extension = (ROOT / "browser-extension" / "manifest.json").read_text()
    adapters = (ROOT / "browser-extension" / "ats-adapters.js").read_text()

    assert "Application Agent is private to the repository owner" in api
    assert "requireMutationRequest(req, res)" in api
    assert "resumeDriveAccess" in api
    assert "application-context.json" in api and "application-context.md" in api
    assert "application-queue.json" in api and "application-issues.md" in api
    assert "application-agent-pairing.json" in api
    assert "DOM dump" in api and "provider session" in api
    assert "awaiting_confirmation" in local and "attestation" in local
    assert "verify_submission_page" in service
    assert "/api/application/form" in service and "/api/application/confirm" in service
    assert '"content_scripts"' in extension and '"<all_urls>"' in extension
    for provider in ("workday", "greenhouse", "lever", "ashby", "smartrecruiters"):
        assert provider in adapters
    assert "JOB_RADAR_SUBMISSION_APPROVED" in (ROOT / "browser-extension" / "content.js").read_text()
    assert "/api/application-agent" in routing and "application_agent=1" in routing
