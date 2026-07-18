from radar.company_info import context


def test_known_defense_company_has_clear_context():
    assert context("Lockheed Martin", "other") == (
        "defense & aerospace", "defense and space systems")


def test_unknown_company_never_displays_other():
    industry, _ = context("Completely Unknown Co", "other")
    assert industry == "industrial engineering"
