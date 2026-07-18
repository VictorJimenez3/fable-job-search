import time

from radar.models import Job
from radar.score import (RULES_VERSION, explicit_new_grad, gates, regate,
                         role_bucket, score, update_feedback_from_applied)

NOW = int(time.time())
FB = {"company_boosts": {}, "token_boosts": {}, "negative_companies": []}


def mk(title, company="Acme", locations=None, source="simplify", desc="", **kw):
    return Job(company=company, title=title, url="https://x.com/j", source=source,
               locations=locations or ["New York, NY"], description=desc, **kw)


def test_accepts_core_chemical_engineering_internships():
    for title in [
        "Chemical Engineering Intern",
        "Process Engineering Intern",
        "Process Development Co-op",
        "Manufacturing Engineering Intern",
        "Bioprocess Intern",
        "Materials Engineering Intern",
        "Environmental Engineering Intern",
        "Quality Engineering Intern",
    ]:
        keep, alert_ok, reasons = gates(mk(title))
        assert keep and alert_ok, title
        assert "internship/co-op title" in reasons


def test_role_bucket_covers_cheme_families():
    expected = {
        "Chemical Engineering Intern": "chemical_process",
        "Upstream Bioprocess Intern": "bioprocess_pharma",
        "Manufacturing Engineering Co-op": "manufacturing_ops",
        "Polymer Materials Intern": "materials_semiconductor",
        "Wastewater Engineering Intern": "environmental_safety",
        "Process Validation Intern": "quality_validation",
        "Engineering Intern": "general_engineering",
    }
    for title, bucket in expected.items():
        assert role_bucket(title) == bucket, title


def test_generic_engineering_uses_description_for_specific_family():
    assert role_bucket("Engineering Intern", "Support fermentation and downstream processing") == \
        "bioprocess_pharma"


def test_requires_internship_or_coop_evidence():
    keep, alert_ok, reasons = gates(mk("Process Engineer"))
    assert keep is False and alert_ok is False
    assert reasons == ["not an internship/co-op"]


def test_rejects_senior_phd_and_non_us():
    assert gates(mk("Senior Process Engineering Intern"))[0] is False
    assert gates(mk("PhD Chemical Engineering Intern"))[0] is False
    assert gates(mk("Process Engineering Intern", locations=["Toronto, Canada"]))[0] is False
    assert gates(mk("Process Engineering Intern", locations=["Bangalore, India"]))[0] is False


def test_rejects_clearance_and_large_experience_requirement():
    assert gates(mk("Process Engineering Intern", desc="Active TS/SCI clearance required."))[0] is False
    assert gates(mk("Process Engineering Intern",
                    desc="Requires 5+ years of professional experience."))[0] is False


def test_adjacent_engineering_internships_are_dashboard_only():
    for title in ["Mechanical Engineering Intern", "Electrical Engineering Intern"]:
        keep, alert_ok, reasons = gates(mk(title, sector="chemicals_materials"))
        assert keep is True and alert_ok is False, title
        assert any("off-field" in r for r in reasons), title


def test_unrelated_internships_are_rejected():
    for title in ["Software Process Engineering Intern", "AI Engineering Intern",
                  "Sales Engineering Intern", "Marketing Intern"]:
        keep, alert_ok, reasons = gates(mk(
            title, company="Dow",
            desc="Dow builds chemical processes and advanced materials."))
        assert keep is False and alert_ok is False
        assert any("off-field internship" in r for r in reasons)


def test_generic_engineering_requires_target_sector_for_alert():
    keep, alert_ok, _ = gates(mk("Engineering Intern", sector="chemicals_materials"))
    assert keep and alert_ok
    keep, alert_ok, reasons = gates(mk("Engineering Intern", sector="other"))
    assert keep and alert_ok is False
    assert any("generic engineering" in r for r in reasons)


def test_generic_fallback_does_not_auto_alert_named_other_disciplines():
    keep, alert_ok, reasons = gates(mk(
        "Failure Analysis Engineer Intern", sector="consumer_manufacturing"))
    assert keep and alert_ok is False
    assert any("needs review" in r for r in reasons)

    keep, alert_ok, _ = gates(mk("Data Engineering Intern", sector="pharma_biotech"))
    assert keep is False and alert_ok is False


def test_explicit_compatibility_flag_means_internship_on_this_branch():
    assert explicit_new_grad("Chemical Engineering Intern") is True
    assert explicit_new_grad("Process Engineer") is False


def test_score_prefers_process_role_in_priority_sector():
    target = mk("Chemical Engineering Intern", company="Dow")
    target.sector = "chemicals_materials"
    target.posted_at = NOW - 3600
    score(target, FB, NOW)

    generic = mk("Engineering Intern", company="RandomCo")
    generic.sector = "other"
    generic.posted_at = NOW - 6 * 86400
    score(generic, FB, NOW)

    assert target.score > generic.score + 20
    assert any("role:chemical_process" in r for r in target.score_reasons)
    assert any("sector:chemicals_materials" in r for r in target.score_reasons)
    assert any("internship title" in r for r in target.score_reasons)


def test_score_prefers_bioprocess_to_generic_engineering():
    bio = mk("Bioprocess Engineering Intern")
    generic = mk("Engineering Intern")
    score(bio, FB, NOW)
    score(generic, FB, NOW)
    assert bio.score > generic.score


def test_feedback_boosts_applied_companies():
    fb = {"company_boosts": {}, "token_boosts": {}, "negative_companies": []}
    update_feedback_from_applied(fb, "Dow", "Process Engineering Intern")
    j = mk("Process Engineering Intern", company="Dow")
    score(j, fb, NOW)
    assert any("engaged with" in r for r in j.score_reasons)


def test_negative_feedback_penalizes():
    fb = {"company_boosts": {}, "token_boosts": {}, "negative_companies": ["acme"]}
    j = mk("Process Engineering Intern")
    j2 = mk("Process Engineering Intern")
    score(j, fb, NOW)
    score(j2, FB, NOW)
    assert j.score == j2.score - 10


def test_cheme_feedback_tokens_are_learnable():
    fb = {"company_boosts": {}, "token_boosts": {}, "negative_companies": []}
    update_feedback_from_applied(fb, "Acme", "Quality Operations Intern")
    assert "quality" in fb["token_boosts"]
    assert "operations" in fb["token_boosts"]


def _stored(title, company="Dow", **over):
    rec = {"id": title, "company": company, "title": title, "url": "https://x.com/j",
           "source": "greenhouse", "locations": ["New York, NY"], "salary": "",
           "remote": False, "sector": "chemicals_materials", "score": 80,
           "score_reasons": ["base 40"], "alert_ok": True}
    rec.update(over)
    return rec


def test_regate_demotes_inherited_tech_alerts_and_promotes_cheme():
    jobs = {
        "old": _stored("Software Engineer, New Grad", company="OpenAI", sector="ai_lab"),
        "target": _stored("Chemical Engineering Intern", alert_ok=False),
        "closed": _stored("Process Engineering Intern", closed_at=NOW, rules_v=1),
        "current": _stored("Process Engineering Intern", rules_v=RULES_VERSION),
    }
    flipped = regate(jobs)
    assert jobs["old"]["alert_ok"] is False
    assert jobs["target"]["alert_ok"] is True
    assert jobs["target"]["explicit_internship"] is True
    assert jobs["closed"]["rules_v"] == 1
    assert jobs["current"]["alert_ok"] is True
    assert flipped == 2


def test_regate_reapplies_quality_suppression():
    jobs = {"q": _stored(
        "Process Engineering Intern", alert_ok=False,
        quality={"checked_at": NOW, "live": True, "new_grad": "no",
                 "years_required": 5, "role_family": "chemical-process", "reason": "x"})}
    regate(jobs)
    assert jobs["q"]["alert_ok"] is False
    assert any("not internship-level" in r for r in jobs["q"]["score_reasons"])
