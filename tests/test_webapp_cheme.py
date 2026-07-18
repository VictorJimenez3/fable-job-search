from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_pages_mirror_matches_canonical_platform():
    assert (ROOT / "webapp/index.html").read_bytes() == \
        (ROOT / "docs/platform/index.html").read_bytes()


def test_platform_exposes_cheme_application_filters_and_actions():
    html = (ROOT / "webapp/index.html").read_text()
    for expected in (
        'aria-label="Role family"',
        'aria-label="Visa sponsorship"',
        'aria-label="Experience"',
        'sponsorship stated',
        '3+ years stated',
        'open application ↗',
        'f_JT:"I"',
        'CULTURE_MODE = "chemical_engineering_internship"',
    ):
        assert expected in html


def test_platform_does_not_classify_every_title_as_general_engineering():
    html = (ROOT / "webapp/index.html").read_text()
    assert 'return "other";' in html
    assert 'Object.values(S.jobs).filter(isChemeVisible)' in html


def test_platform_filters_explicit_foreign_locations_directly():
    html = (ROOT / "webapp/index.html").read_text()
    assert "function isUSLocation(j)" in html
    assert "j.closed_at || !isUSLocation(j)" in html
    for evidence in ("ontario", "brockville", "SGP", "GBR"):
        assert evidence in html


def test_platform_hides_generic_tech_internships_from_cheme_roles():
    html = (ROOT / "webapp/index.html").read_text()
    for evidence in ("deep learning", "java", "gpu programming",
                     "quantitative research", "forward deployed engineer"):
        assert evidence in html
