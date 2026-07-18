import json
import time

from radar import company_research as research


NOW = int(time.time())


def test_excerpt_is_small_relevant_and_drops_boilerplate():
    text = ("Acme Health is a care-navigation company serving university students. "
            "Our mission is to make clinical care easier to access. "
            "We build a platform used by patients and care teams. "
            "All qualified applicants receive equal opportunity. "
            "The salary range is $90,000 to $160,000.")
    excerpt = research.extract_excerpt("Acme Health", text)
    assert "care-navigation" in excerpt and "Our mission" in excerpt
    assert "equal opportunity" not in excerpt and "salary range" not in excerpt
    assert len(excerpt) <= 600


def test_capture_is_bounded_and_identity_is_exact():
    records = {}
    changed = research.capture_into(
        records, company="Acme Health", title="Software Engineer",
        url="https://acme.example/jobs/1",
        text="Acme Health is a care platform for students. " * 8,
        retrieved_at=NOW)
    assert changed and len(records["acme health"]["sources"]) == 1
    assert not research.capture_into(
        records, company="Acme Health", title="Software Engineer",
        url="https://acme.example/jobs/1",
        text="Acme Health is a care platform for students. " * 8,
        retrieved_at=NOW + 60)
    assert research.dossier_for("Acme Health", records)
    assert research.dossier_for("Acme Holdings", records) is None
    records["acme health"]["aliases"] = ["Acme Care"]
    assert research.dossier_for("Acme Care", records)


def test_parser_downgrades_uncited_claims_and_rejects_bad_response():
    raw = json.dumps({
        field: {"value": "Not confirmed", "source_ids": [], "confidence": "low"}
        for field in research.FIELDS
    } | {
        "summary": {"value": "Builds clinical software", "source_ids": ["S1"],
                    "confidence": "high"},
        "products": {"value": "Care platform", "source_ids": ["S1"],
                     "confidence": "medium"},
        "customers": {"value": "Hospitals", "source_ids": ["MADE_UP"],
                      "confidence": "high"},
    })
    parsed = research.parse_synthesis(raw, {"S1"})
    assert parsed["summary"]["source_ids"] == ["S1"]
    assert parsed["customers"]["value"] == "Not confirmed"
    assert research.parse_synthesis("not json", {"S1"}) is None


def test_enrich_is_grounded_cached_and_priority_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(research.state, "STATE_DIR", tmp_path)
    records = {}
    research.capture_into(
        records, company="Acme Health", title="New Grad Engineer",
        url="https://acme.example/jobs/1",
        text=("Acme Health is a care platform for university students. "
              "Our mission is to make healthcare easier to access. ") * 5,
        retrieved_at=NOW)
    research.save(records)
    source_id = records["acme health"]["sources"][0]["id"]
    body = {field: {"value": "Not confirmed", "source_ids": [], "confidence": "low"}
            for field in research.FIELDS}
    body["summary"] = {"value": "Care-access software for students",
                       "source_ids": [source_id], "confidence": "high"}
    body["products"] = {"value": "Care navigation platform",
                        "source_ids": [source_id], "confidence": "medium"}
    calls = {"n": 0}

    def complete(*args, **kwargs):
        calls["n"] += 1
        text = json.dumps(body)
        assert kwargs["validator"](text)
        return text

    monkeypatch.setattr(research.llm, "available", lambda task="general": True)
    monkeypatch.setattr(research.llm, "complete", complete)
    monkeypatch.setattr(research.llm, "usage_report", lambda: {"events": [
        {"endpoint": "nvidia:nemotron", "model": "nemotron"}]})
    jobs = {"j1": {"id": "j1", "company": "Acme Health", "score": 90,
                    "alert_ok": True, "first_seen": NOW}}
    assert research.enrich(jobs, [{"id": "j1", "stage": "saved"}], {}, limit=1) == 1
    assert research.load()["acme health"]["status"] == "ready"
    assert research.enrich(jobs, [], {}, limit=1) == 0
    assert calls["n"] == 1
