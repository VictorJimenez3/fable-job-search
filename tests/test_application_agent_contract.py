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
    assert "async function recoverQueue" in api and 'action === "recover"' in api
    assert "paired Mac access required" in api
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
    assert '"version": "0.3.2"' in extension
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
    assert 'from "./application-fields.mjs"' in background
    assert "resumeFileAccepted" in background
    assert "Pretending every file input" in background
    assert "never overwrite an input that already has a file after a rescan" in background
    assert "localAnswerShouldSync" in background and "reusableAnswerSignature" in background
    assert "answer.reusable === false" in background and "localBeforeImport" in background
    assert "answer.queue_ids || []" in background and "essay_context" in background
    assert "confirmedSameReview" in background
    assert "owner_approved_at" in background and "approval_expires_at" in background
    assert "last_message || session.last_error || session.state" in background
    assert "Owner requested a fresh application-page scan." in background
    assert "use an explicit rescan before changing the page" in local
    assert "Resume uploaded; waiting for the employer form to validate the PDF" in content
    assert "element.files?.[0]?.name === file.name" in content
    assert "rememberedUploadedFile" in content and "fillsThisPass" in content
    assert "directApplicationURL" in content and "directApplicationUrl" in background
    assert "Do not click Next in the same turn as a file assignment" in content
    assert "Review ready · no changes applied" in content
    assert "recoverOrphanedQueue" in background
    assert "applicationIdentity" in background and "claimedTabIds" in background
    assert "localQueueRecoveryItems" in background and 'action: "recover"' in background
    assert "RECOVERABLE_QUEUE_STATES" in background
    assert "recovered: recoveredCount" in background
    assert "recovery_reset" in background
    assert "recovered" in (ROOT / "browser-extension" / "popup.js").read_text()
    assert 'parkedButAttachable = ["blocked", "awaiting_confirmation"]' in background
    assert "lastAccessed" in background
    assert 'cloudAnswer.value !== answer.value' in background
    assert "repairTrackedTabs" in background
    assert "navigateQueuedTab" in background
    assert "Chrome did not navigate the paired tab" in background
    assert "withinAttachGrace" in background
    assert "chrome.tabs.update" in background
    assert "reused: true" in background
    assert "chrome.tabs.remove" in background
    assert "JOB_RADAR_AGENT_STATUS" in background
    assert "JOB_RADAR_AGENT_STATUS" in content
    assert 'document.querySelectorAll("#job-radar-application-agent")' in content
    assert "existingBanners.slice(1).forEach(node => node.remove())" in content
    assert "box-sizing:border-box" in content
    assert "overflow-wrap:anywhere" in content
    assert "function conflictingChoiceFills" in content
    assert "Application paused · conflicting choices" in content
    assert "if (element.checked === truthy) return true" in content
    assert "if (String(element.value || \"\") === String(value || \"\")) return true" in content
    assert "JOB_RADAR_PAGE_BLOCKED" in background
    assert 'if (row && message.pageFailure)' in background
    assert "reconcileTerminalTabs" in background
    assert "JOB_RADAR_AGENT_STOP" in background
    assert "chrome.runtime.onStartup.addListener(scheduleQueueSync)" in background
    assert "scheduleQueueSync();" in background
    assert "chrome.runtime.reload()" in background
    assert "JOB_RADAR_RELOAD_EXTENSION" in background
    assert "JOB_RADAR_STATUS" in background and "requestExtensionStatus" in background
    assert "syncCloudQueueIfEnabled" in background
    assert "automationEnabled: false" in background
    assert "automationEnabled: true" in background
    assert "extensionVersion: chrome.runtime.getManifest().version" in background
    assert "RADAR_PAGE_ORIGINS" in background
    assert "Only the Job Radar popup or owner Job Radar page can request a reload." in background
    popup = (ROOT / "browser-extension" / "popup.js").read_text()
    popup_html = (ROOT / "browser-extension" / "popup.html").read_text()
    assert 'send("JOB_RADAR_RELOAD_EXTENSION")' in popup
    assert 'id="reload"' in popup_html and 'id="version"' in popup_html
    assert "job-radar:extension-command" in content
    assert "JOB_RADAR_SYNC_NOW" in content
    assert 'status: "JOB_RADAR_STATUS"' in content
    assert "job not found" in content
    assert "job (?:has |is )?closed" in content
    assert "page (?:you are looking for )?" in content
    assert "const blocked = await say({type: \"JOB_RADAR_PAGE_BLOCKED\"" in content
    assert "pageFailure: unavailable" in content
    assert "JOB_RADAR_AGENT_STOP" in content
    assert "scanRunning" in content and "scanAgain" in content
    assert "LOOP_SCAN_LIMIT" in content and "Application paused · repeated form cycle" in content
    assert "field.options, field.value" in content
    assert 'type === "textarea" || element.isContentEditable ? 20_000' in content
    assert "const remembered = uploadedFiles.get(exactKey)" in content
    assert 'if (isRadar)' in content and 'job-radar:agent-started' in content
    assert content.index('job-radar:agent-started') < content.index("return;\n  }", content.index('job-radar:agent-started'))
    assert "posting_status" in api
    assert "no longer open" in api
    assert "collapseActiveQueueDuplicates" in api and "queueJobIdentity" in api
    assert 'payload.action === "queue_many"' in api
    assert "importTrackerChoices" in background and "trackerChoiceTime" in background
    assert 'action: "queue_many"' in background
    assert "/api/application/tracker-sync" in background and "request_tracker_sync" in service
    assert "_collapse_active_session_duplicates" in local
    assert "application_identity" in local
    frontend = (ROOT / "webapp" / "index.html").read_text()
    assert "controlApplicationAgentExtension" in frontend
    assert "probeApplicationAgentExtension" in frontend
    assert "Mac ready · paused" in frontend
    assert "sendApplicationPairingToExtension" in frontend
    assert "applicationAgentURL" in frontend
    assert 'command==="sync_queue"?30000' in frontend
    assert "retireTerminalApplicationQueueItems" in frontend
    assert "extension v${esc(data.extensionVersion)}" in frontend
    assert "jr_extension_reload_pending" in frontend and "reconnectApplicationAgentAfterReload" in frontend
    assert "queueing…" in frontend
    assert "queueActionId" in frontend
    assert "expandedQueueId" in frontend and "toggleApplicationAgentDetails" in frontend
    assert 'aria-expanded="${expanded}"' in frontend
    assert "!isTerminalPosting(job)" in frontend
    assert "/api/application-agent" in routing and "application_agent=1" in routing
    assert "compactApplicationReviewFields" in frontend
    assert ".agent-review-field > strong, .agent-review-field > .sub" in frontend
    assert "cloudReturnedEmpty" in frontend
    assert "reconcileAutopilotChoices" in frontend and "queueAutopilotJobs" in frontend
    assert "Private Google Sheet connected." in frontend
    assert 'category:"essay_context"' in frontend and "queue_ids:[queueId]" in frontend
    assert "Drive file storage is full" not in frontend
