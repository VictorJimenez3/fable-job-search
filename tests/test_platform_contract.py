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
