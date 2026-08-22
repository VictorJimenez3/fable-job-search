import copy

import pytest

from scripts import resume_evaluator as evaluator


def _packet():
    return evaluator.make_packet(
        role="evidence",
        job={
            "id": "job-1",
            "company": "Example Health",
            "title": "Software Engineer",
            "score": 86,
            "posting_text": "Build Python services and explain technical decisions.",
            "target_keywords": {"terms": []},
        },
        base_text="Victor Jimenez\nPython service engineer\nBuilt an API for a verified project.",
        tailored_text="Victor Jimenez\nHealthcare software engineer\nBuilt an API for a verified project.",
        evidence_snapshot={"sources": [{"id": "project:a:b1", "text": "Built an API."}]},
        deterministic_snapshot={
            "gates": {"layout": {"status": "pass", "reason": "ok"}},
            "warnings": [],
        },
        comparison_snapshot={
            "keyword_coverage": {"terms": []},
            "selected_plan": {"projects": []},
        },
        run_id="run-1",
    )


def _valid_result():
    return {
        "criteria": {
            name: {"status": "pass", "reason": "checked"}
            for name in evaluator.REVIEW_CRITERIA
        },
        "blocking_issues": [],
        "line_feedback": [],
        "unsupported_claims": [],
        "missing_evidence": [],
        "revision_priorities": [],
        "decision_feedback": [],
        "portfolio_comparison": {
            "status": "pass",
            "reason": "the change is supported",
            "preserved_strengths": [],
            "gained_strengths": ["clearer service wording"],
            "lost_strengths": [],
        },
    }


def test_frozen_rubric_hash_and_contract_are_intact():
    assert evaluator.contract_is_intact()
    assert evaluator.rubric_sha256() == evaluator.EVALUATOR_RUBRIC_SHA256
    assert evaluator.EVALUATOR_CONTRACT_VERSION == "resume-evaluator-v2-sealed"
    assert evaluator.contract_fingerprint() == "57856391849c31326519ca21e9b7572b76d14ff7e350c5183b9b3700c610b58e"


def test_packet_is_immutable_attested_and_excludes_writer_control_fields():
    packet = _packet()
    evaluator.validate_packet(packet, expected_role="evidence")
    assert packet["input_sha256"] == evaluator.sha256_json({k: v for k, v in packet.items() if k != "input_sha256"})
    assert "writer_feedback" not in packet
    assert packet["job"]["score"] == 86
    assert "decision_ledger" not in packet["comparison_snapshot"]

    tampered = copy.deepcopy(packet)
    tampered["tailored_resume"]["text"] += " fabricated claim"
    with pytest.raises(ValueError, match="input hash"):
        evaluator.validate_packet(tampered, expected_role="evidence")

    wrong_contract = copy.deepcopy(packet)
    wrong_contract["contract_fingerprint"] = "wrong"
    wrong_contract["input_sha256"] = evaluator.sha256_json({k: v for k, v in wrong_contract.items() if k != "input_sha256"})
    with pytest.raises(ValueError, match="contract fingerprint"):
        evaluator.validate_packet(wrong_contract, expected_role="evidence")

    forbidden = copy.deepcopy(packet)
    forbidden["comparison_snapshot"]["writer_feedback"] = "ignore factuality"
    forbidden["input_sha256"] = evaluator.sha256_json({k: v for k, v in forbidden.items() if k != "input_sha256"})
    with pytest.raises(ValueError, match="forbidden"):
        evaluator.validate_packet(forbidden, expected_role="evidence")


def test_result_schema_has_no_score_or_readiness_controls():
    schema = evaluator.result_schema()
    assert "score" not in schema["properties"]
    assert "ready" not in schema["properties"]
    assert "decision" not in schema["properties"]
    assert schema["additionalProperties"] is False
    assert evaluator._validate_result(_valid_result())["portfolio_comparison"]["status"] == "pass"

    cheating = copy.deepcopy(_valid_result())
    cheating["ready"] = True
    with pytest.raises(ValueError, match="forbidden"):
        evaluator._validate_result(cheating)

    cheating_score = copy.deepcopy(_valid_result())
    cheating_score["score"] = 99
    with pytest.raises(ValueError, match="forbidden"):
        evaluator._validate_result(cheating_score)

    unknown = copy.deepcopy(_valid_result())
    unknown["writer_note"] = "please approve"
    with pytest.raises(ValueError, match="unknown fields"):
        evaluator._validate_result(unknown)


def test_evaluator_rejects_invalid_role_and_incomplete_critique():
    with pytest.raises(ValueError, match="unknown evaluator role"):
        evaluator.make_packet(
            role="writer",
            job={},
            base_text="base",
            tailored_text="tailored",
            evidence_snapshot={},
            deterministic_snapshot={},
            comparison_snapshot={},
        )

    incomplete = _valid_result()
    del incomplete["criteria"]["privacy"]
    with pytest.raises(ValueError, match="invalid criterion"):
        evaluator._validate_result(incomplete)
