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
