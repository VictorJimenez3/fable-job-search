from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_platform_mirror_matches_canonical_frontend():
    canonical = (ROOT / "webapp" / "index.html").read_bytes()
    mirror = (ROOT / "docs" / "platform" / "index.html").read_bytes()
    assert mirror == canonical


def test_platform_defaults_to_a_fresh_entry_compatible_action_queue():
    html = (ROOT / "webapp" / "index.html").read_text()
    assert 'experience:"entryfit"' in html
    assert 'bestWindow:"2592000"' in html
    assert "Fresh action queue." in html
    assert "Expired and filled postings are removed from active Jobs" in html
    assert "posting-specific verdicts preserved" in (ROOT / "radar" / "score.py").read_text()


def test_platform_exposes_owner_batch_resume_tailoring_for_today():
    html = (ROOT / "webapp" / "index.html").read_text()
    assert "Tailor today" in html
    assert "RESUME_BATCH_LIMIT = 12" in html
    assert "function resumeBatchCandidates()" in html
    assert "function queueResumeBatch()" in html
    assert "private drafts · no applications sent" in html
    assert "local calendar day" in html
    assert '"to_tailor"' in html
    assert 'value="to_tailor"' in html
    assert "Queueing moves selected roles into the Pipeline’s" in html
    assert "stage_to_tailor" in (ROOT / "profile.yaml").read_text()
    assert "to_tailor" in (ROOT / "webapp" / "api" / "_google-tracker.js").read_text()
    assert "STAGE_ORDER = TRACKER_STAGE_ORDER" in (ROOT / "radar" / "email_watch.py").read_text()
    assert "to_tailor" in (ROOT / "webapp" / "api" / "v1" / "_applications.js").read_text()


def test_platform_exposes_synced_notion_tailor_batch_and_protected_resume_editor():
    html = (ROOT / "webapp" / "index.html").read_text()
    bank = (ROOT / "webapp" / "api" / "resume-bank.js").read_text()
    assert "Autopilot To tailor" in html
    assert "function resumeNotionTailorCandidates()" in html
    assert "RESUME_NOTION_BATCH_LIMIT = 500" in html
    assert "latest synced Notion state" in html
    assert "Notion → Resume Studio → Application Autopilot" in html
    assert "window.confirm" not in html
    assert "queueAutopilotJobs(jobs)" in html
    assert "const workerCount = engineOnline ? Math.min(4, jobs.length) : 1" in html
    assert "Edit resume" in html
    assert "original PDF stays untouched" in html
    assert "no editable source" in html
    assert "MAX_QUEUE_ITEMS = 500" in bank


def test_platform_defensively_hides_closed_link_signal_and_exposes_family_identity():
    html = (ROOT / "webapp" / "index.html").read_text()
    dedupe = (ROOT / "radar" / "dedupe.py").read_text()
    assert '"closed"' in html and "link_resolution" in html
    assert "postingVariantBadge" in html
    assert "collapse_location_variants" in dedupe
    assert "posting_family_id" in dedupe


def test_outreach_uses_public_search_links_without_linkedin_scraping():
    html = (ROOT / "webapp" / "index.html").read_text()
    assert "function recruiterDorks(j)" in html
    assert "site:linkedin.com/in" in html
    assert "site:linkedin.com/posts" in html
    assert "https://www.google.com/search?" in html
    assert "the radar never scrapes LinkedIn" in html


def test_platform_exposes_dol_sponsor_history_context_and_filter():
    html = (ROOT / "webapp" / "index.html").read_text()
    assert "sponsorship_history" in html
    assert "DOL sponsor history" in html
    assert "likely historical sponsor" in html
    assert "not a promise that this role sponsors" in html


def test_platform_exposes_private_google_tracker_path_for_authenticated_users():
    html = (ROOT / "webapp" / "index.html").read_text()
    api = (ROOT / "webapp" / "api" / "tracker.js").read_text()
    helper = (ROOT / "webapp" / "api" / "_google-tracker.js").read_text()
    action = (ROOT / "webapp" / "api" / "action.js").read_text()
    main = (ROOT / "radar" / "main.py").read_text()
    assert 'fetch("/api/tracker")' in html
    assert "User Applications" in api + helper
    assert "Google Token Ciphertext" in helper
    assert "hasPersonalSession" in helper
    assert "findPersonalTracker" in helper
    assert "personal_tracker" in helper
    assert "s.pt" in api
    assert "trackerPanelHTML" in html
    assert "trackerActionButton" in html
    assert "Open Google Sheet" in html
    assert "Notion is your default tracker" in html
    assert "optional mirror" in html
    assert 'jr_tracker_backend' in html
    assert "tracker-quick" in html
    assert "separate from other users" in html
    assert "setPipelineStage" in html
    assert '"stage"' in helper + action + main


def test_platform_exposes_terminal_posting_history_and_private_status_sync():
    html = (ROOT / "webapp" / "index.html").read_text()
    helper = (ROOT / "webapp" / "api" / "_google-tracker.js").read_text()
    lifecycle = (ROOT / "radar" / "lifecycle.py").read_text()
    assert '["history","History"]' in html
    assert "function rHistory()" in html
    assert "syncTrackerLifecycle" in html
    assert "posting_status" in html + helper + lifecycle
    assert '"Posting Status"' in helper
    assert "expired" in helper and "filled" in helper
    assert "RADAR_HISTORY_DAYS" in lifecycle


def test_application_history_shows_posting_duration_instead_of_a_date():
    html = (ROOT / "webapp" / "index.html").read_text()
    assert "function postingDurationLabel(job)" in html
    assert "posting was up for" in html
    assert '"less than 1 day"' in html
    assert "month${months === 1" in html and "year${years === 1" in html


def test_platform_exposes_isolated_internship_lane_and_graduation_preferences():
    html = (ROOT / "webapp" / "index.html").read_text()
    helper = (ROOT / "webapp" / "api" / "_google-tracker.js").read_text()
    tracker = (ROOT / "webapp" / "api" / "tracker.js").read_text()
    workflow = (ROOT / ".github" / "workflows" / "internship-radar.yml").read_text()
    batch = (ROOT / ".github" / "workflows" / "internship-alert-batch.yml").read_text()
    assert 'id="laneSwitch"' in html
    assert "Internships" in html
    assert "state/intern_jobs.json" in html
    assert "state/intern_web_state.json" in html
    assert "expectedGraduation" in html
    assert "freshman" in html and "sophomore" in html and "junior" in html and "senior" in html
    assert '"Internships"' in helper
    assert '"Preferences"' in helper
    assert "expected_graduation" in helper
    assert "profile=${encodeURIComponent(mode)}" in html
    assert "profileOf" in tracker
    assert "RADAR_PROFILE: internship" in workflow
    assert "RADAR_PROFILE: internship" in batch
    assert "RADAR_DEFER_DELIVERY" in workflow
    assert "internship_email" in html + (ROOT / "radar/board.py").read_text()


def test_internship_scoring_is_neutral_and_has_coverage_checks():
    html = (ROOT / "webapp" / "index.html").read_text()
    scorer = (ROOT / "radar" / "internship.py").read_text()
    profile = (ROOT / "profiles" / "internship.yaml").read_text()
    workflow = (ROOT / ".github" / "workflows" / "internship-radar.yml").read_text()
    assert "RULES_VERSION = 6" in scorer
    assert "INTERNSHIP_TITLE_RE" in scorer
    assert '"internship_signal"' in scorer
    assert "flat across role families" in scorer
    assert "preference_profile" not in scorer
    assert "personal_signal" not in scorer
    assert "remote +" not in scorer
    assert "curated internship source" not in scorer
    assert "internship_scoring:" in profile
    assert "prestige_tiers:" in profile
    assert "work_quality_cap:" in profile
    assert "compensation_points:" in profile
    assert "prestige_points:" in profile
    assert "python -m radar.main rescore" in workflow
    assert "rescore_only" in workflow
    assert "Rescore stored internship state only" in workflow
    assert "score-health" in workflow
    assert "neutral friend-facing rubric" in html
    assert "work_quality" in html
    assert "Prestige / crackedness" in html
    assert 'name === "prestige"' in html
    assert "full-time review cap" in scorer
    assert "full-time-only signal" in html
    assert "include review-only" in html


def test_platform_exposes_oauth_account_center_and_tutorial():
    html = (ROOT / "webapp" / "index.html").read_text()
    api = "\n".join((ROOT / "webapp" / "api" / name).read_text()
                  for name in ["login.js", "callback.js", "google-callback.js", "config.js"])
    helper = (ROOT / "webapp" / "api" / "_google-tracker.js").read_text()
    assert "openTutorial('accounts')" in html
    assert "Tracker & Sheets" in html
    assert "Connect Google" in html
    assert "openid email profile" in api
    assert "https://www.googleapis.com/auth/drive.file" in api
    assert "https://www.googleapis.com/auth/spreadsheets" not in api
    assert "Google Sheet ID" in helper
    assert "tracker.personalConfigured()" in api
    assert "code_challenge_method" in api
    assert "no password" in html.lower()


def test_platform_keeps_oauth_sessions_in_sync_across_vercel_doors():
    html = (ROOT / "webapp" / "index.html").read_text()
    api_names = ["_lib.js", "login.js", "callback.js", "google-callback.js",
                 "logout.js", "session-handoff.js", "config.js"]
    api = "\n".join((ROOT / "webapp" / "api" / name).read_text() for name in api_names)
    assert "job-radar-newgrad.vercel.app" in api
    assert "auth_host" in api
    assert "session-handoff" in api
    assert "authReturnHost" in api
    assert "sessionHandoffLocation" in api
    assert "sessionCookies" in api
    assert "return_host" in api
    assert "session_handoff" in api + html
    assert "auth=already-signed-in" in api + html
    assert "takeSessionHandoffTicket" in html
    assert "applySessionHandoff" in html
    assert "credentials:\"include\"" in html
    assert "Signed out on both Job Radar URLs" in html
    assert "provider tokens never enter" in html


def test_platform_exposes_owner_taste_and_community_moderation_paths():
    html = (ROOT / "webapp" / "index.html").read_text()
    action = (ROOT / "webapp" / "api" / "action.js").read_text()
    workflow = (ROOT / ".github" / "workflows" / "report-sync.yml").read_text()
    assert "My job preferences" in html
    assert "tasteSimilarityCandidates" in html
    assert "slice(0, 1500)" in html
    assert "source.titleTokens.has" in html
    assert "Similar jobs to inspect" in html
    assert "The Radar learns bounded preference points" in html
    assert "learned title signals" in html
    assert "feedback" in action and "archive" in action
    assert "radar-report:" in html and "three distinct" in html
    assert "github.event.issue.user" in workflow or "radar-report:" in workflow


def test_platform_pm_family_matches_backend_product_management_titles():
    html = (ROOT / "webapp" / "index.html").read_text()
    assert r"product\s+(?:manager|owner|management)" in html
    assert (ROOT / "webapp" / "index.html").read_bytes() == \
        (ROOT / "docs" / "platform" / "index.html").read_bytes()


def test_platform_explains_each_score_dimension_without_hiding_the_ledger():
    html = (ROOT / "webapp" / "index.html").read_text()
    assert "SCORE_DIMENSION_META" in html
    assert "scoreDimensionWhy" in html
    assert "Positive helps; zero means no signal; negative lowers." in html
    assert "Exact reason ledger" in html
    assert "Configured preference override set the final score" in html


def test_platform_job_rows_cycle_saved_green_then_excluded_red():
    html = (ROOT / "webapp" / "index.html").read_text()
    assert "cycleJobSelection" in html
    assert "S.web.excluded" in html
    assert ".job.selected" in html and ".job.excluded" in html
    assert "show excluded" in html
    assert "Excluded from active Jobs" in html


def test_platform_role_field_buttons_cycle_to_red_exclusion_without_disappearing():
    html = (ROOT / "webapp" / "index.html").read_text()
    assert "function cycleRoleFilter" in html
    assert "roleFilterState" in html
    assert "excludedRoles" in html
    assert 'class="toggle ${state}"' in html
    assert "button.toggle.excluded" in html
    assert "twice turns red and excludes" in html
    assert "f.excludedRoles.includes(roleFamily(j))" in html


def test_platform_resume_studio_is_one_cloud_workspace_with_private_engine_fallback():
    html = (ROOT / "webapp" / "index.html").read_text()
    studio = (ROOT / "scripts" / "resume_studio.py").read_text()
    bank = (ROOT / "webapp" / "api" / "resume-bank.js").read_text()
    assert '"Resume Studio"' in html
    assert "RESUME_STUDIO_LOCAL_ORIGIN" in html
    assert "openResumeStudio" in html
    assert "queueResumeMode" in html
    assert "renderResumeBankView" in html
    assert "resumeBankGroups" in html
    assert "click to see every version" in html
    assert "Google onward" in html
    assert "Context &amp; Q&amp;A" in html
    assert "answerContextQuestion" in html
    assert "syncCloudResumeBank" in html
    assert 'fetch("/api/resume-bank"' in html
    assert "Resume bank" in html
    assert "resumeDriveAccess" in bank
    assert "private, no-store" in bank
    assert "Job Radar Resume Bank" in bank
    assert "Resume Bank is private to the repository owner" in bank
    assert "private engine offline" in html
    assert "cloudQueue" in html
    assert "queue_update" in html
    assert "drainCloudResumeQueue" in html
    assert "job_snapshot:cloudQueueJob(item.job)" in html
    assert "the Mac worker will pick it up when it reconnects" in html
    assert "tailor here" in html
    assert "Take-the-wheel (moderate)" in html
    assert "Unchained generation" in html
    assert "function studioModeGuideHTML" in html
    assert "AI takes the lead → Unchained" in html
    assert "Most autonomous" in html
    assert "These are still active modes, not obsolete labels" in html
    assert "Take-the-wheel (moderate)" in studio
    assert "Queue Take-the-wheel" in studio
    assert "DEFAULT_BRIDGE_ORIGINS" in studio
    assert "cross-origin access is disabled" in studio
    assert "Access-Control-Allow-Private-Network" in studio
    assert "directStudioRequest" in html
    assert "start the Mac engine first" in html
    assert "connect Mac engine" in html
    assert "primeResumeBridge" in html
    assert "popup.location.href = bridgeURL" in html
    assert "bridge_nonce" in studio + html
    assert "event.source===window.opener" in studio
    assert "event.source !== resumeBridgeWindow" in html
    assert "cv_present" in studio


def test_platform_boots_progressively_and_keeps_owner_diagnostics_in_app():
    html = (ROOT / "webapp" / "index.html").read_text()
    assert 'id="bootNotice"' in html
    assert 'Loading the radar shell' in html
    assert "function loadState" in html
    assert 'allowMissing && r.status === 404' in html
    assert 'path === "state/reports.json"' in html
    assert "Promise.allSettled" in html
    assert 'loadState("state/jobs.json", {}, {critical:true})' in html
    assert "retry failed loads" in html
    assert "ownerDevMode" in html
    assert 'victorjimenez3' in html
    assert "No email or external issue was sent" in html
    assert "fetchJSON" not in html


def test_owner_only_resume_studio_is_integrated_with_job_selection():
    html = (ROOT / "webapp" / "index.html").read_text()
    assert 'const RESUME_STUDIO_ORIGIN = "http://127.0.0.1:4317/"' in html
    assert "function ownerResumeStudio" in html
    assert 'norm(S.auth.login) === "victorjimenez3"' in html
    assert "function openResumeStudio" in html
    assert "resumeStudioSnapshot" in html
    assert "privateWeb.jd || j.description" in html
    assert 'actTrack(j, true).then' not in html
    assert 'feedback-description' in html
    assert "startupStageInfo" in html
    assert 'state:${code}' in html
    assert 'role not added to To apply' in html
    assert '["testing","Resume Studio"]' in html
    assert '[["resume","Resume"]]' in html
    assert "save + open Resume Studio" in html
    assert "window.openResumeStudio = openResumeStudio" in html
