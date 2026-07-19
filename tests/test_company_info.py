from radar.company_info import context
from radar import company_info


def test_known_defense_company_has_clear_context():
    assert context("Lockheed Martin", "other") == (
        "defense & aerospace", "defense and space systems")


def test_unknown_company_never_displays_other():
    industry, _ = context("Completely Unknown Co", "other")
    assert industry == "general technology"


def test_snapshot_surfaces_grounded_mission_and_culture(monkeypatch):
    monkeypatch.setattr(company_info, "research_for", lambda _: {
        "status": "ready", "mission": {"value": "help patients", "source_ids": ["s"]}
    })
    monkeypatch.setattr(company_info, "dossier_for", lambda *_: {
        "pace": "fast", "vibe": "high ownership", "pto": "20 days"
    })
    text = company_info.snapshot("Acme Health")
    assert "mission: help patients" in text
    assert "pace: fast" in text and "PTO: 20 days" in text
