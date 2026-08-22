#!/usr/bin/env python3
"""Sealed, critique-only Resume Studio evaluator.

This module is intentionally separate from the resume writer.  The writer
never supplies the evaluator's rubric, score, prior critique, or readiness
state.  The parent process supplies one immutable packet containing the job,
the rendered base and candidate resumes, authoritative evidence, and
deterministic checks.  The evaluator can report findings only; deterministic
code decides whether the panel is complete and whether a draft is shippable.

The CLI is invoked in a fresh read-only Codex process with a scratch working
directory and ``--skip-git-repo-check``.  It does not inspect the repository or
the writer's prompts/reports.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


EVALUATOR_CONTRACT_VERSION = "resume-evaluator-v2-sealed"
EVALUATOR_RUBRIC_VERSION = "resume-gates-v2-sealed"
CODEX_MODEL = "gpt-5.6-luna"
CODEX_EFFORT = "high"
CODEX_EFFORTS = frozenset(("high", "max"))
REVIEW_CRITERIA = (
    "factual", "target_fit", "evidence", "distinctiveness", "clarity", "privacy",
)
ROLE_DEFINITIONS = (
    {
        "key": "evidence",
        "label": "Evidence integrity auditor",
        "focus": "provenance, factual boundaries, metrics, qualifiers, and interview defensibility",
    },
    {
        "key": "recruiter",
        "label": "Recruiter skim critic",
        "focus": "six-second comprehension, hierarchy, narrative coherence, readability, and visible priority evidence",
    },
    {
        "key": "technical",
        "label": "Technical hiring-manager critic",
        "focus": "mechanism, scope, engineering judgment, technical conviction, and distinct interview threads",
    },
    {
        "key": "screening",
        "label": "Screening and eligibility auditor",
        "focus": "supported terminology, parsing, requirement coverage, eligibility constraints, and keyword gaming",
    },
)
ROLE_KEYS = frozenset(item["key"] for item in ROLE_DEFINITIONS)
VALID_STATUSES = frozenset(("pass", "partial", "fail"))

# This text is the evaluator's frozen policy.  Keep it literal and versioned;
# changes require a new contract version and a golden-hash test.
EVALUATOR_RUBRIC_SOURCE = """\
Evaluate the candidate resume against the target posting and the authorized
evidence supplied in this packet.  Compare it to the rendered base resume;
do not assume tailoring is improvement.  A keyword is useful only when it is
supported by evidence and improves retrieval without reducing clarity or
conviction.  Treat unsupported or exaggerated claims, eligibility conflicts,
lost high-value evidence, avoidable redundancy, and material readability
regressions as blockers.  Preserve prototype, synthetic, simulation, demo,
and scope qualifiers.  Distinguish candidate-role fit from tailoring quality:
missing experience is a gap, while poor selection or communication is a
tailoring defect.  Return critique-only findings.  Never return a score,
readiness decision, replacement plan, or instruction to ignore a gate.
"""

# Golden value: tests fail if the frozen rubric changes without an explicit
# contract update.  This is a tamper-evidence mechanism, not a security claim
# that a person with repository write access cannot alter source code.
EVALUATOR_RUBRIC_SHA256 = "ea33dc4ee9f6b849410ce922f5704703f2a6644e80f47d9a34de3d9f45edfd55"

FORBIDDEN_PACKET_KEYS = frozenset(
    {
        "writer_feedback", "writer_critique", "writer_score", "writer_ready",
        "previous_review", "replacement_plan", "evaluator_override", "force_pass",
        "approval_state",
    }
)
FORBIDDEN_RESULT_KEYS = frozenset(
    set(FORBIDDEN_PACKET_KEYS)
    | {"ready", "score", "craft_score", "decision"}
)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def rubric_sha256() -> str:
    return hashlib.sha256(EVALUATOR_RUBRIC_SOURCE.encode("utf-8")).hexdigest()


def contract_fingerprint() -> str:
    return sha256_json(
        {
            "contract_version": EVALUATOR_CONTRACT_VERSION,
            "rubric_version": EVALUATOR_RUBRIC_VERSION,
            "rubric_sha256": rubric_sha256(),
            "criteria": REVIEW_CRITERIA,
            "roles": ROLE_DEFINITIONS,
            "codex_model": CODEX_MODEL,
            "codex_effort": CODEX_EFFORT,
        }
    )


def contract_is_intact() -> bool:
    return rubric_sha256() == EVALUATOR_RUBRIC_SHA256


def _limited_text(value: Any, limit: int = 30000) -> str:
    return str(value or "")[:limit]


def _safe_job(job: Dict[str, Any]) -> Dict[str, Any]:
    allowed = (
        "id", "company", "title", "url", "locations", "sector", "description",
        "posting_text", "target_keywords", "job_intelligence", "alert_ok",
        "early_career_possible", "score",
    )
    return {
        key: job.get(key)
        for key in allowed
        if key in job and key not in FORBIDDEN_PACKET_KEYS
    }


def _walk_forbidden(
    value: Any,
    path: str = "packet",
    forbidden_keys: Iterable[str] = FORBIDDEN_PACKET_KEYS,
) -> Optional[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().lower()
            if normalized in forbidden_keys:
                return "%s.%s" % (path, key)
            found = _walk_forbidden(child, "%s.%s" % (path, key), forbidden_keys)
            if found:
                return found
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found = _walk_forbidden(child, "%s[%d]" % (path, index), forbidden_keys)
            if found:
                return found
    return None


def make_packet(
    *,
    role: str,
    job: Dict[str, Any],
    base_text: str,
    tailored_text: str,
    evidence_snapshot: Any,
    deterministic_snapshot: Dict[str, Any],
    comparison_snapshot: Dict[str, Any],
    run_id: str = "",
) -> Dict[str, Any]:
    if role not in ROLE_KEYS:
        raise ValueError("unknown evaluator role: %s" % role)
    if not contract_is_intact():
        raise RuntimeError("sealed evaluator rubric hash mismatch")
    packet = {
        "contract_version": EVALUATOR_CONTRACT_VERSION,
        "contract_fingerprint": contract_fingerprint(),
        "rubric_version": EVALUATOR_RUBRIC_VERSION,
        "rubric_sha256": EVALUATOR_RUBRIC_SHA256,
        "role": role,
        "run_id": str(run_id or ""),
        "job": _safe_job(job),
        "base_resume": {
            "sha256": hashlib.sha256(str(base_text).encode("utf-8")).hexdigest(),
            "text": _limited_text(base_text, 50000),
        },
        "tailored_resume": {
            "sha256": hashlib.sha256(str(tailored_text).encode("utf-8")).hexdigest(),
            "text": _limited_text(tailored_text, 50000),
        },
        "evidence_snapshot": evidence_snapshot,
        "deterministic_snapshot": deterministic_snapshot,
        "comparison_snapshot": comparison_snapshot,
    }
    packet["input_sha256"] = sha256_json(packet)
    return packet


def validate_packet(packet: Any, expected_role: str = "") -> None:
    if not isinstance(packet, dict):
        raise ValueError("evaluator packet must be an object")
    if not contract_is_intact():
        raise RuntimeError("sealed evaluator rubric hash mismatch")
    if packet.get("contract_version") != EVALUATOR_CONTRACT_VERSION:
        raise ValueError("evaluator contract version mismatch")
    if packet.get("contract_fingerprint") != contract_fingerprint():
        raise ValueError("evaluator contract fingerprint mismatch")
    if packet.get("rubric_version") != EVALUATOR_RUBRIC_VERSION:
        raise ValueError("evaluator rubric version mismatch")
    if packet.get("rubric_sha256") != EVALUATOR_RUBRIC_SHA256:
        raise ValueError("evaluator rubric hash mismatch")
    role = str(packet.get("role") or "")
    if role not in ROLE_KEYS or expected_role and role != expected_role:
        raise ValueError("evaluator role mismatch")
    forbidden = _walk_forbidden(packet)
    if forbidden:
        raise ValueError("forbidden writer/evaluator control field: %s" % forbidden)
    required = (
        "job", "base_resume", "tailored_resume", "evidence_snapshot",
        "deterministic_snapshot", "comparison_snapshot", "input_sha256",
    )
    for key in required:
        if key not in packet:
            raise ValueError("evaluator packet missing %s" % key)
    unsigned = dict(packet)
    supplied_hash = unsigned.pop("input_sha256", None)
    if supplied_hash != sha256_json(unsigned):
        raise ValueError("evaluator input hash mismatch")


def result_schema() -> Dict[str, Any]:
    criterion = {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["pass", "partial", "fail"]},
            "reason": {"type": "string"},
        },
        "required": ["status", "reason"],
        "additionalProperties": False,
    }
    line_feedback = {
        "type": "object",
        "properties": {
            "source_id": {"type": "string"},
            "issue": {"type": "string"},
            "recommendation": {"type": "string"},
            "severity": {"type": "string"},
        },
        "required": ["source_id", "issue", "recommendation", "severity"],
        "additionalProperties": False,
    }
    decision_feedback = {
        "type": "object",
        "properties": {
            "action": {"type": "string"},
            "current_evidence": {"type": "string"},
            "replacement_or_exclusion": {"type": "string"},
            "issue": {"type": "string"},
            "recommendation": {"type": "string"},
            "severity": {"type": "string"},
        },
        "required": [
            "action", "current_evidence", "replacement_or_exclusion", "issue",
            "recommendation", "severity",
        ],
        "additionalProperties": False,
    }
    properties = {
        "criteria": {
            "type": "object",
            "properties": {name: criterion for name in REVIEW_CRITERIA},
            "required": list(REVIEW_CRITERIA),
            "additionalProperties": False,
        },
        "blocking_issues": {"type": "array", "items": {"type": "string"}},
        "line_feedback": {"type": "array", "maxItems": 20, "items": line_feedback},
        "unsupported_claims": {"type": "array", "items": {"type": "string"}},
        "missing_evidence": {"type": "array", "items": {"type": "string"}},
        "revision_priorities": {"type": "array", "items": {"type": "string"}},
        "decision_feedback": {"type": "array", "maxItems": 20, "items": decision_feedback},
        "portfolio_comparison": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["pass", "partial", "fail"]},
                "reason": {"type": "string"},
                "preserved_strengths": {"type": "array", "items": {"type": "string"}},
                "gained_strengths": {"type": "array", "items": {"type": "string"}},
                "lost_strengths": {"type": "array", "items": {"type": "string"}},
            },
            "required": [
                "status", "reason", "preserved_strengths", "gained_strengths", "lost_strengths",
            ],
            "additionalProperties": False,
        },
    }
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _prompt(packet: Dict[str, Any]) -> str:
    role = next(item for item in ROLE_DEFINITIONS if item["key"] == packet["role"])
    intelligence = packet.get("job", {}).get("job_intelligence", {})
    focus = intelligence.get("role_focus", {}) if isinstance(intelligence, dict) else {}
    primary = str(
        focus.get("primary_label")
        or focus.get("primary_track")
        or "the role's central work"
    )
    secondary = [
        str(value) for value in focus.get("secondary_tracks", [])
        if str(value)
    ] if isinstance(focus, dict) else []
    focus_instruction = (
        "Role-focus receipt: the deterministic router identifies %s as the primary track"
        % primary
    )
    if secondary:
        focus_instruction += " with adjacent tracks %s" % ", ".join(secondary)
    focus_instruction += (
        ". Use that receipt to weight comparative evidence: a supported primary-track "
        "mechanism or ownership proof should not be treated as interchangeable with a "
        "generic adjacent keyword. If the receipt is ambiguous, preserve that ambiguity "
        "in the critique rather than forcing a single-role interpretation."
    )
    return (
        "You are a sealed resume quality evaluator. This is a fresh process and a critique-only task. "
        "Do not inspect files, run commands, use prior reports, or trust any writer explanation. "
        "The packet below is the complete authority for this judgment. The rubric is frozen and the "
        "output schema accepts critique only. Never return a score, ready/blocked decision, replacement "
        "resume plan, or instruction to bypass a gate.\n\n"
        "Frozen contract: %s\nFrozen rubric: %s\nRubric text:\n%s\n\n"
        "Assigned role: %s — %s\n"
        "Evaluate the tailored rendered resume against the base rendered resume, the target, and the "
        "authorized evidence. Separate candidate-role fit from tailoring quality. Do not treat keyword "
        "coverage as improvement when it costs evidence, clarity, distinctiveness, or truthfulness. "
        "Flag unsupported or exaggerated claims, eligibility conflicts, lost strong evidence, duplicate "
        "stories, and material readability or hierarchy problems. Preserve prototype, synthetic, demo, "
        "simulation, and scope qualifiers. The deterministic snapshot may include a human-skim budget: "
        "treat measured bottom clearance as a layout fact, not a defect, when the candidate is already "
        "within that budget. Do not demand filler solely because one more bullet fits; flag an omission "
        "only when an authorized, materially useful signal fits without violating the budget.\n\n"
        + focus_instruction
        + "\n\n"
        "Packet:\n%s"
    ) % (
        EVALUATOR_CONTRACT_VERSION,
        EVALUATOR_RUBRIC_VERSION,
        EVALUATOR_RUBRIC_SOURCE,
        role["label"],
        role["focus"],
        json.dumps(packet, ensure_ascii=False, sort_keys=True),
    )


def _extract_json(raw: str) -> Optional[Dict[str, Any]]:
    raw = str(raw or "").strip()
    candidates = [raw, re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I | re.S)]
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(raw[start:end + 1])
        except ValueError:
            return None
        return value if isinstance(value, dict) else None
    return None


def _provider_env(run_dir: Path) -> Dict[str, str]:
    env = dict(os.environ)
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        env.pop(key, None)
    env["RESUME_STUDIO_EVALUATOR_RUN_DIR"] = str(run_dir)
    return env


def _stop(proc: subprocess.Popen) -> None:
    """Stop only the Codex child owned by this evaluator process.

    The parent Resume Studio launcher already places this evaluator in its
    own process group.  The Codex child must therefore stay in that inherited
    group so a parent timeout can reap the whole evaluator tree.  Killing the
    child's process group here would also kill this evaluator (and can race
    the parent launcher), while creating a new session leaves an orphan after
    the outer launcher is stopped.  Direct termination is the correct inner
    boundary; the outer process-group cleanup handles launcher timeouts.
    """
    try:
        if proc.poll() is not None:
            return
        proc.terminate()
        proc.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            proc.kill()
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _validate_result(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("evaluator did not return an object")
    forbidden = _walk_forbidden(value, forbidden_keys=FORBIDDEN_RESULT_KEYS)
    if forbidden:
        raise ValueError("evaluator returned forbidden control field: %s" % forbidden)
    expected = set(result_schema()["properties"])
    unknown = sorted(set(value) - expected)
    if unknown:
        raise ValueError("evaluator returned unknown fields: %s" % ", ".join(unknown[:8]))
    missing = sorted(expected - set(value))
    if missing:
        raise ValueError("evaluator omitted required fields: %s" % ", ".join(missing[:8]))
    criteria = value.get("criteria")
    if not isinstance(criteria, dict):
        raise ValueError("evaluator result has no criteria")
    extra_criteria = sorted(set(criteria) - set(REVIEW_CRITERIA))
    if extra_criteria:
        raise ValueError("evaluator criterion has unknown fields: %s" % ", ".join(extra_criteria[:8]))
    for name in REVIEW_CRITERIA:
        item = criteria.get(name)
        if not isinstance(item, dict) or item.get("status") not in VALID_STATUSES or not isinstance(item.get("reason"), str):
            raise ValueError("evaluator result has invalid criterion: %s" % name)
        if set(item) != {"status", "reason"}:
            raise ValueError("evaluator criterion has unknown fields: %s" % name)
    comparison = value.get("portfolio_comparison")
    if not isinstance(comparison, dict) or comparison.get("status") not in VALID_STATUSES:
        raise ValueError("evaluator result has invalid portfolio comparison")
    comparison_fields = {"status", "reason", "preserved_strengths", "gained_strengths", "lost_strengths"}
    if set(comparison) != comparison_fields:
        raise ValueError("evaluator portfolio comparison has unknown fields")
    for field in (
        "blocking_issues", "line_feedback", "unsupported_claims", "missing_evidence",
        "revision_priorities", "decision_feedback",
    ):
        if not isinstance(value.get(field), list):
            raise ValueError("evaluator result field is not an array: %s" % field)
    for item in value["line_feedback"]:
        if not isinstance(item, dict) or set(item) != {"source_id", "issue", "recommendation", "severity"}:
            raise ValueError("evaluator line feedback has invalid fields")
    decision_fields = {
        "action", "current_evidence", "replacement_or_exclusion", "issue",
        "recommendation", "severity",
    }
    for item in value["decision_feedback"]:
        if not isinstance(item, dict) or set(item) != decision_fields:
            raise ValueError("evaluator decision feedback has invalid fields")
    return value


def evaluate_packet(
    packet_path: Path, output_path: Path, scratch: Path, timeout: int,
    effort: str = CODEX_EFFORT,
) -> int:
    requested_effort = str(effort or CODEX_EFFORT).strip().lower()
    if requested_effort not in CODEX_EFFORTS:
        requested_effort = CODEX_EFFORT
    try:
        packet = json.loads(packet_path.read_text())
        validate_packet(packet, expected_role=str(packet.get("role") or ""))
        if not contract_is_intact():
            raise RuntimeError("sealed evaluator rubric hash mismatch")
        codex = shutil.which("codex")
        if not codex:
            raise RuntimeError("Codex CLI is not installed")
        scratch.mkdir(parents=True, exist_ok=True)
        schema_path = scratch / "schema.json"
        stdout_path = scratch / "codex.stdout.json"
        stderr_path = scratch / "codex.stderr.txt"
        schema_path.write_text(json.dumps(result_schema(), indent=2, sort_keys=True))
        args = [
            codex, "exec", "-c", "model=" + CODEX_MODEL,
            "-c", "model_reasoning_effort=" + requested_effort,
            "--sandbox", "read-only", "--ephemeral", "--skip-git-repo-check",
            "--output-schema", str(schema_path), "-o", str(stdout_path), "-",
        ]
        started = time.time()
        with stderr_path.open("w") as err, stdout_path.open("w") as out:
            proc = subprocess.Popen(
                args, cwd=str(scratch), env=_provider_env(scratch), stdin=subprocess.PIPE,
                stdout=out, stderr=err, text=True, start_new_session=False,
            )
            try:
                proc.stdin.write(_prompt(packet))
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            while proc.poll() is None:
                if time.time() - started >= timeout:
                    _stop(proc)
                    raise TimeoutError("sealed evaluator timed out after %ss" % timeout)
                time.sleep(0.25)
        raw = stdout_path.read_text(errors="replace") if stdout_path.exists() else ""
        result = _validate_result(_extract_json(raw) or {})
        wrapped = {
            "provider": "codex",
            "execution_lane": "sealed_evaluator",
            "ok": True,
            "role": packet["role"],
            "contract_version": EVALUATOR_CONTRACT_VERSION,
            "contract_fingerprint": contract_fingerprint(),
            "rubric_sha256": EVALUATOR_RUBRIC_SHA256,
            "input_sha256": packet["input_sha256"],
            "reasoning_effort": requested_effort,
            "elapsed_seconds": round(time.time() - started, 1),
            "data": result,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
        }
    except Exception as exc:  # noqa: BLE001 - durable failure record is the contract
        wrapped = {
            "provider": "codex",
            "execution_lane": "sealed_evaluator",
            "ok": False,
            "role": str(packet.get("role") if isinstance(locals().get("packet"), dict) else ""),
            "contract_version": EVALUATOR_CONTRACT_VERSION,
            "contract_fingerprint": contract_fingerprint() if contract_is_intact() else "",
            "reasoning_effort": requested_effort,
            "error": str(exc),
        }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(wrapped, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if wrapped.get("ok") else 1


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--scratch", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=8 * 60)
    parser.add_argument("--effort", choices=sorted(CODEX_EFFORTS), default=CODEX_EFFORT)
    args = parser.parse_args(argv)
    return evaluate_packet(
        args.packet, args.output, args.scratch, max(30, args.timeout), args.effort,
    )


if __name__ == "__main__":
    raise SystemExit(main())
