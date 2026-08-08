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
    assert 'fetch("/api/tracker")' in html
    assert "User Applications" in api + helper
    assert "Google Token Ciphertext" in helper
    assert "hasPersonalSession" in helper
    assert "findPersonalTracker" in helper
    assert "personal_tracker" in helper
    assert "s.pt" in api
    assert "other users cannot read it" in html


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
