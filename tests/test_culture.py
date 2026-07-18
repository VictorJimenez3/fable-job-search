import pytest

from radar import culture, state
from radar.models import Job
from radar.score import score


@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    return tmp_path


def test_seed_loads_and_scores(tmp_state):
    dossiers = culture.load()
    n = culture.sync_seed(dossiers)
    assert n >= 30
    li = culture.dossier_for("LinkedIn", dossiers)
    meta = culture.dossier_for("Meta", dossiers)
    # LinkedIn (wlb 5, shutdowns, moderate) must beat Meta (wlb 2) on the
    # user's stated criteria despite Meta's higher prestige/pay
    assert li["fit"] > meta["fit"]
    assert all(0 <= d["fit"] <= 100 for d in dossiers.values())


def test_fit_score_math():
    d = {"prestige": "S", "wlb": 5, "pace": "fast", "shutdowns": "Y: winter",
         "new_grad_tc": "$220k"}
    assert culture.fit_score(d) == 100
    d = {"prestige": "C", "wlb": 1, "pace": "deliberate", "shutdowns": "N",
         "new_grad_tc": "$85k"}
    assert culture.fit_score(d) <= 15


def test_dossier_loose_match(tmp_state):
    dossiers = culture.load()
    culture.sync_seed(dossiers)
    assert culture.dossier_for("Uber Technologies, Inc.", dossiers)["name"] == "Uber"
    assert culture.dossier_for("Totally Unknown Startup", dossiers) is None


def test_alert_tag_and_render(tmp_state):
    dossiers = culture.load()
    culture.sync_seed(dossiers)
    tag = culture.alert_tag("Pinterest", dossiers)
    assert "fit" in tag and "wlb" in tag
    md = culture.render_md(dossiers)
    assert "| Fit |" in md and "Pinterest" in md
    reply = culture.render_dossier_reply(culture.dossier_for("Pinterest", dossiers))
    assert "culture fit" in reply and "Rotational" in reply


def test_only_curated_culture_feeds_ranking(monkeypatch):
    from radar import score as score_mod
    dossier = {"name": "GreatCo", "fit": 100, "source": "seed"}
    monkeypatch.setattr(score_mod, "_CULTURE_CACHE", {"greatco": dossier})
    fb = {"company_boosts": {}, "token_boosts": {}, "negative_companies": []}
    j1 = Job(company="GreatCo", title="Software Engineer, New Grad",
             url="u", source="simplify", locations=["Remote"])
    j2 = Job(company="PlainCo", title="Software Engineer, New Grad",
             url="u", source="simplify", locations=["Remote"])
    score(j1, fb)
    score(j2, fb)
    assert j1.score == j2.score + 6
    assert any("culture fit" in r for r in j1.score_reasons)

    # Model-memory estimates remain visible but cannot silently move ranking.
    monkeypatch.setattr(score_mod, "_CULTURE_CACHE",
                        {"greatco": {**dossier, "source": "est."}})
    j3 = Job(company="GreatCo", title="Software Engineer, New Grad",
             url="u", source="simplify", locations=["Remote"])
    score(j3, fb)
    assert j3.score == j2.score


def test_enrich_missing_generates_via_llm(tmp_state, monkeypatch):
    import json as _json
    from radar import llm
    state.save("alert_history.json", [
        {"id": "x1", "company": "MysteryHealth AI", "title": "MLE", "alerted_at": 1}])
    fake = _json.dumps({"industry": "healthtech", "prestige": "A", "pace": "fast",
                        "wlb": 4, "vibe": "small and mighty", "pto": "unlimited",
                        "shutdowns": "N", "new_grad_tc": "$150k (est.)",
                        "rotational": "N", "fit_notes": "good fit"})
    monkeypatch.setenv("LLM_BASE_URL", "http://fake")
    monkeypatch.setattr(llm, "complete", lambda *a, **k: fake)
    monkeypatch.setattr(culture.llm, "complete", lambda *a, **k: fake)
    made = culture.enrich_missing()
    assert made == 1
    d = culture.dossier_for("MysteryHealth AI")
    assert d["source"] == "est." and d["fit"] > 50
    # idempotent: second pass generates nothing
    assert culture.enrich_missing() == 0
