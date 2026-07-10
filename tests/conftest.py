import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def _no_culture_bleed(monkeypatch):
    """score() consults the culture-dossier cache; default it to empty so
    repo state never changes unrelated test outcomes. Culture tests set
    their own cache explicitly."""
    from radar import score
    monkeypatch.setattr(score, "_CULTURE_CACHE", {})
