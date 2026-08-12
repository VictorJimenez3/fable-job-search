from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_platform_mirror_matches_canonical_frontend():
    canonical = (ROOT / "webapp" / "index.html").read_bytes()
    mirror = (ROOT / "docs" / "platform" / "index.html").read_bytes()
    assert mirror == canonical


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
    assert 'actTrack(j, true).then' in html
    assert '["testing","Resume Studio"]' in html
    assert '[["resume","Resume"]]' in html
    assert "save + open Resume Studio" in html
    assert "window.openResumeStudio = openResumeStudio" in html
