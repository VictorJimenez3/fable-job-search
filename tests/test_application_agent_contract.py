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
    assert "resumeStorageAccess" in api
    assert "Application Agent" in api and "Payload JSON" in api
    assert "writeSheetStore" in api and 'storage: "sheet"' in api
    assert "SHEET_CACHE_TTL_MS" in api and "saveContextBatch" in api
    assert "automation_runs" in api and 'storage: "database"' in api
    assert "application-context.json" in api and "application-context.md" in api
    assert "application-queue.json" in api and "application-issues.md" in api
    assert "application-agent-pairing.json" in api
    assert 'job-radar-application-pairing' in api
    assert 'unseal(token)' in api
    assert 'access.current?.pt' in api
    assert "DOM dump" in api and "provider session" in api
    assert "awaiting_confirmation" in local and "attestation" in local
    assert "verify_submission_page" in service
    assert "/api/application/form" in service and "/api/application/confirm" in service
    assert "application_resume_status" in service and "/api/application/resume" in service
    assert "application_resume_file" in service and "/api/application/resume-file" in service
    assert "application_essay_answer" in service and "warm-scholarship-essay" in service
    assert '"content_scripts"' in extension and '"<all_urls>"' in extension
    for provider in ("workday", "greenhouse", "lever", "ashby", "smartrecruiters"):
        assert provider in adapters
    assert "JOB_RADAR_SUBMISSION_APPROVED" in (ROOT / "browser-extension" / "content.js").read_text()
    assert "ensureResumeForApplication" in (ROOT / "browser-extension" / "background.js").read_text()
    assert "ensureApplicationSession" in (ROOT / "browser-extension" / "background.js").read_text()
    assert "JOB_RADAR_RESUME_STATUS" in (ROOT / "browser-extension" / "content.js").read_text()
    assert "Google Sheets is rate-limited" in (ROOT / "browser-extension" / "background.js").read_text()
    assert 'action: "answers"' in (ROOT / "browser-extension" / "background.js").read_text()
    background = (ROOT / "browser-extension" / "background.js").read_text()
    content = (ROOT / "browser-extension" / "content.js").read_text()
    assert "localFile" in background and "maxConcurrentApplications" in background
    assert "row?.sessionId && row?.resumeFile" in background
    assert "!row.sessionId || !row.resumeFile" in background
    assert "new DataTransfer()" in content and "new File(" in content
    assert "resumeFieldsNeedingUpload" in background
    assert "never overwrite an input that already has a file after a rescan" in background
    assert "last_message || session.last_error || session.state" in background
    assert "Owner requested a fresh application-page scan." in background
    assert "use an explicit rescan before changing the page" in local
    assert "Resume uploaded; waiting for the employer form to validate the PDF" in content
    assert "element.files?.[0]?.name === file.name" in content
    assert "Do not click Next in the same turn as a file assignment" in content
    assert "recoverOrphanedQueue" in background
    assert 'parkedButAttachable = ["blocked", "awaiting_confirmation"]' in background
    assert "lastAccessed" in background
    assert 'cloudAnswer.value !== answer.value' in background
    assert "repairTrackedTabs" in background
    assert "navigateQueuedTab" in background
    assert "Chrome did not navigate the paired tab" in background
    assert "withinAttachGrace" in background
    assert "chrome.tabs.update" in background
    assert "chrome.tabs.reload" in background
    assert "chrome.tabs.remove" in background
    assert "JOB_RADAR_AGENT_STATUS" in background
    assert "JOB_RADAR_AGENT_STATUS" in content
    assert "JOB_RADAR_PAGE_BLOCKED" in background
    assert 'if (row && message.pageFailure)' in background
    assert "reconcileTerminalTabs" in background
    assert "JOB_RADAR_AGENT_STOP" in background
    assert "chrome.runtime.onStartup.addListener(scheduleQueueSync)" in background
    assert "scheduleQueueSync();" in background
    assert "job not found" in content
    assert "job (?:has |is )?closed" in content
    assert "page (?:you are looking for )?" in content
    assert "const blocked = await say({type: \"JOB_RADAR_PAGE_BLOCKED\"" in content
    assert "pageFailure: unavailable" in content
    assert "JOB_RADAR_AGENT_STOP" in content
    assert "scanRunning" in content and "scanAgain" in content
    assert 'if (isRadar)' in content and 'job-radar:agent-started' in content
    assert content.index('job-radar:agent-started') < content.index("return;\n  }", content.index('job-radar:agent-started'))
    assert "posting_status" in api
    assert "no longer open" in api
    frontend = (ROOT / "webapp" / "index.html").read_text()
    assert "queueing…" in frontend
    assert "queueActionId" in frontend
    assert "!isTerminalPosting(job)" in frontend
    assert "/api/application-agent" in routing and "application_agent=1" in routing
