import datetime as dt
import json
import os
from pathlib import Path

import pytest

from scripts import resume_studio as rs
from scripts import resume_lock
from radar.evidence_review import (add_question_hint, answer_question, dismiss_question_hint, load_reviews,
                                   owner_answer_nodes, upsert_questions)


def test_extract_json_handles_provider_wrapper_and_fences():
    raw = json.dumps({"result": "```json\n{\"resume_tex\": \"hello\"}\n```"})
    assert rs.extract_json(raw) == {"resume_tex": "hello"}


def test_response_data_preserves_structured_plan():
    raw = json.dumps({"experiences": [{"source_id": "experience:a"}], "projects": []})
    data = rs.response_data(raw)
    assert data["experiences"][0]["source_id"] == "experience:a"


def test_sealed_panel_requires_each_attested_role_exactly_once():
    roles = [
        {"key": "evidence"},
        {"key": "recruiter"},
    ]
    records = [
        {"critic_role": "evidence", "ok": True, "execution_lane": "sealed_evaluator", "contract_version": rs.SEALED_EVALUATOR_CONTRACT},
        {"critic_role": "evidence", "ok": True, "execution_lane": "sealed_evaluator", "contract_version": rs.SEALED_EVALUATOR_CONTRACT},
        {"critic_role": "recruiter", "ok": True, "execution_lane": "writer_provider", "contract_version": rs.SEALED_EVALUATOR_CONTRACT},
    ]
    status = rs.sealed_panel_status(records, roles)
    assert status["complete"] is False
    assert status["completed_roles"] == ["evidence"]
    assert status["failed_roles"] == ["recruiter"]


def test_sealed_evaluator_uses_disposable_cwd_and_persists_attestation(tmp_path, monkeypatch):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_codex = fake_bin / "codex"
    fake_codex.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "out = sys.argv[sys.argv.index('-o') + 1]\n"
        "result = {'criteria': {name: {'status': 'pass', 'reason': 'checked'} for name in ('factual', 'target_fit', 'evidence', 'distinctiveness', 'clarity', 'privacy')}, 'blocking_issues': [], 'line_feedback': [], 'unsupported_claims': [], 'missing_evidence': [], 'revision_priorities': [], 'decision_feedback': [], 'portfolio_comparison': {'status': 'pass', 'reason': 'checked', 'preserved_strengths': [], 'gained_strengths': [], 'lost_strengths': []}}\n"
        "open(out, 'w').write(json.dumps(result))\n"
    )
    fake_codex.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ.get("PATH", ""))
    packet = rs.resume_evaluator.make_packet(
        role="evidence", job={"company": "Example", "title": "Engineer"},
        base_text="base", tailored_text="tailored", evidence_snapshot={},
        deterministic_snapshot={}, comparison_snapshot={}, run_id="run-1",
    )
    run_dir = tmp_path / "run"
    result = rs.run_sealed_evaluator(packet, run_dir, "evidence", timeout=30, evaluator_effort="high")
    assert result["ok"] is True
    assert result["execution_lane"] == "sealed_evaluator"
    assert result["contract_version"] == rs.SEALED_EVALUATOR_CONTRACT
    assert result["reasoning_effort"] == "high"
    assert Path(result["stdout_path"]).exists()
    assert not (run_dir / "sealed_evaluator" / "evidence_scratch").exists()


def test_write_json_uses_an_atomic_worker_specific_temp_file(tmp_path):
    target = tmp_path / "value.json"
    rs.write_json(target, {"ok": True})
    assert json.loads(target.read_text()) == {"ok": True}
    assert list(tmp_path.glob("*.tmp")) == []


def test_compile_resume_resolves_relative_run_dir_before_invoking_tectonic(tmp_path, monkeypatch):
    run_dir = tmp_path / "relative-run"
    run_dir.mkdir()
    (run_dir / "resume.tex").write_text("\\documentclass{article}\\begin{document}ok\\end{document}")
    calls = {}

    def fake_run(args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        run_dir.joinpath("resume.pdf").write_bytes(b"pdf")
        return type("Completed", (), {"returncode": 0, "stdout": "compiled"})()

    monkeypatch.chdir(tmp_path.parent)
    monkeypatch.setattr(rs.shutil, "which", lambda name: "/usr/bin/tectonic" if name == "tectonic" else None)
    monkeypatch.setattr(rs.subprocess, "run", fake_run)

    result = rs.compile_resume(Path(tmp_path.name) / "relative-run")

    assert result["compiled"] is True
    assert calls["kwargs"]["cwd"] == str(run_dir.resolve())
    assert str(run_dir.resolve()) in calls["args"]


def test_context_questions_group_a_capability_across_postings_and_authorize_only_used_answers(tmp_path):
    studio = tmp_path / "CV" / ".resume_studio"
    upsert_questions(studio, [{
        "term": "version control", "job_id": "job-1", "company": "Acme",
        "title": "Engineer", "importance": "required",
    }])
    upsert_questions(studio, [{
        "term": "Version Control", "job_id": "job-2", "company": "Example",
        "title": "Developer", "importance": "preferred",
    }])
    payload = load_reviews(studio)
    assert len(payload["questions"]) == 1
    item = next(iter(payload["questions"].values()))
    assert len(item["triggers"]) == 2

    answer_question(
        studio, item["id"], "used",
        answer="Managed feature branches, pull requests, and reviewed changes with Git and GitHub.",
        where_when="J&J internship, summer 2025",
    )
    nodes = owner_answer_nodes(load_reviews(studio))
    assert len(nodes) == 1
    assert nodes[0]["claim_allowed"] is True
    assert nodes[0]["source"] == "Victor Q&A"
    assert "J&J internship" in nodes[0]["text"]


def test_context_not_used_answer_is_remembered_but_never_becomes_evidence(tmp_path):
    studio = tmp_path / "CV" / ".resume_studio"
    upsert_questions(studio, [{"term": "databricks", "job_id": "job-1"}])
    item = next(iter(load_reviews(studio)["questions"].values()))
    answer_question(studio, item["id"], "not_used")
    saved = load_reviews(studio)["questions"][item["id"]]
    assert saved["status"] == "answered"
    assert saved["response"] == "not_used"
    assert owner_answer_nodes(load_reviews(studio)) == []


def test_context_place_hint_is_durable_but_never_authorizes_a_claim(tmp_path):
    studio = tmp_path / "CV" / ".resume_studio"
    upsert_questions(studio, [{"term": "ci/cd", "job_id": "job-1"}])
    item = next(iter(load_reviews(studio)["questions"].values()))
    add_question_hint(
        studio, item["id"], "CS485 · Nexus LinkedIn clone",
        note="Check whether the course project used an automated workflow.",
        source_url="https://github.com/example/nexus",
    )
    saved = load_reviews(studio)["questions"][item["id"]]
    assert saved["hints"][0]["claim_allowed"] is False
    assert owner_answer_nodes(load_reviews(studio)) == []


def test_context_place_can_be_ruled_out_without_closing_the_capability(tmp_path):
    studio = tmp_path / "CV" / ".resume_studio"
    upsert_questions(studio, [{"term": "ci/cd", "job_id": "job-1"}])
    item = next(iter(load_reviews(studio)["questions"].values()))
    dismiss_question_hint(studio, item["id"], "Johnson & Johnson internship")
    saved = load_reviews(studio)["questions"][item["id"]]
    assert saved["status"] == "open"
    assert saved["dismissed_hints"] == ["Johnson & Johnson internship"]
    assert owner_answer_nodes(load_reviews(studio)) == []


def test_context_candidates_turn_neighboring_evidence_into_questions_not_claims():
    question = {"term": "ci/cd", "hints": []}
    graph = {"nodes": [{
        "id": "jnj:b1", "entry_id": "jnj", "heading": "Johnson & Johnson internship",
        "source": "CV/resume.tex", "text": "Built and tested an AlloyDB deployment pipeline",
        "authority": 90, "claim_allowed": True,
    }]}
    hints = rs._context_candidate_hints(question, graph)
    assert hints[0]["label"] == "Johnson & Johnson internship"
    assert hints[0]["claim_allowed"] is False
    assert "not proof" in hints[0]["reason"]
    assert "personally configure" in hints[0]["question"]
    question["dismissed_hints"] = ["Johnson & Johnson internship"]
    assert rs._context_candidate_hints(question, graph) == []


def test_generated_resume_filename_is_company_identifiable():
    assert rs.resume_pdf_filename({"company": "Johnson & Johnson"}) == "johnson_johnson_resume_ai.pdf"
    assert rs.resume_pdf_filename({"company": "NVIDIA"}) == "nvidia_resume_ai.pdf"
    assert rs.resume_pdf_filename({}) == "company_resume_ai.pdf"
    assert rs.resume_pdf_filename({"company": "Merck"}, "generation") == "merck_resume_unchained.pdf"


def test_generation_mode_is_separate_from_the_saved_moderate_mode():
    assert rs.normalize_tailor_mode("unrestricted") == "unrestricted"
    assert rs.normalize_tailor_mode("unchained") == "generation"
    assert rs.tailor_mode_label("unrestricted") == "Take-the-wheel (moderate)"
    assert rs.tailor_mode_label("generation") == "Unchained generation"
    assert 'id="generation"' in rs.UI_HTML
    assert "Requirement → evidence map" in rs.UI_HTML


def test_project_heading_uses_pipe_separator():
    heading = r"\\textbf{PostureMax --- 1st Place Overall} | \\emph{Python, Flask}"
    assert rs._project_heading(heading) == r"\\textbf{PostureMax | 1st Place Overall} | \\emph{Python, Flask}"


def test_future_one_page_prefix_suppresses_footer_number_without_mutating_template():
    template = "\\fancyfoot[C]{\\footnotesize\\thepage}\n" + rs.BODY_MARKER
    generated = rs._generated_one_page_prefix(template)
    assert rs.GENERATED_ONE_PAGE_FOOTER in generated
    assert rs.CANONICAL_PAGE_FOOTER not in generated
    assert rs.CANONICAL_PAGE_FOOTER in template


def test_pdf_preview_download_name_is_safe_and_company_identifiable():
    assert rs._download_filename("mayo_clinic_rochester_resume_ai.pdf") == "mayo_clinic_rochester_resume_ai.pdf"
    assert rs._download_filename("../../resume.pdf") == "company_resume_ai.pdf"
    assert rs._download_filename("Mayo Clinic resume.pdf") == "Mayo_Clinic_resume.pdf"


def test_resume_report_exposes_change_and_layout_safety_language():
    assert "What changed" in rs.UI_HTML
    assert "rewritten lines" in rs.UI_HTML
    assert "Supported but missing" in rs.UI_HTML
    assert "near-wraps" in rs.UI_HTML
    assert "roomy lines" in rs.UI_HTML
    assert "layout.horizontal.near_wrap_count" in rs.UI_HTML
    assert "ATS overlay" in rs.UI_HTML
    assert "Highlighted review render" in rs.UI_HTML
    assert "low-value paraphrase" in rs.UI_HTML
    assert "Provider flow, model, and usage" in rs.UI_HTML
    assert "Measured page use" in rs.UI_HTML
    assert "Experience chronology preserved" in rs.UI_HTML
    assert "Tailoring decision" in rs.UI_HTML
    assert "Compared with the original resume for this posting" in rs.UI_HTML


def test_portfolio_search_uses_distinct_editorial_hypotheses():
    cards = rs.portfolio_search_variant_cards({}, limit=3)
    assert [item["id"] for item in cards] == [
        "control_preserver", "product_integration", "systems_ml",
    ]
    assert len({item["instruction"] for item in cards}) == 3
    assert all("keyword" not in item["instruction"].lower() or "generic domain keyword" in item["instruction"].lower() for item in cards)


def test_portfolio_search_only_qualifies_complete_positive_tailored_wins():
    variant = {"id": "control_preserver", "label": "Control"}
    base_audit = {
        "recommended_version": "tailored", "decision": "prefer_tailored",
        "review": {"available": True}, "finding_counts": {},
        "comparison": {"gain_weight": 4, "loss_weight": 0, "missed_opportunity_weight": 0},
    }
    incomplete = rs.portfolio_search_candidate_summary(
        variant,
        {"winner_version": "tailored", "tailoring_audit": base_audit,
         "critic_panel": {"all_required_roles": False}},
    )
    assert incomplete["eligible_positive_win"] is False
    assert incomplete["complete_panel"] is False

    complete = rs.portfolio_search_candidate_summary(
        variant,
        {"winner_version": "tailored", "tailoring_audit": base_audit,
         "critic_panel": {"all_required_roles": True}},
    )
    assert complete["eligible_positive_win"] is True
    assert complete["complete_panel"] is True
    assert complete["critic_hard_fail"] is False

    hard_failed = rs.portfolio_search_candidate_summary(
        variant,
        {"winner_version": "tailored", "tailoring_audit": base_audit,
         "review": {"hard_fail": True},
         "critic_panel": {"all_required_roles": True}},
    )
    assert hard_failed["critic_hard_fail"] is True
    assert hard_failed["eligible_positive_win"] is False


def test_portfolio_search_does_not_treat_review_or_base_as_a_win():
    variant = {"id": "systems_ml", "label": "Systems"}
    for winner, recommendation, decision in (
        ("base", "base", "prefer_base"),
        ("base", "review", "needs_review"),
        ("tailored", "review", "needs_review"),
    ):
        summary = rs.portfolio_search_candidate_summary(
            variant,
            {
                "winner_version": winner,
                "tailoring_audit": {
                    "recommended_version": recommendation,
                    "decision": decision,
                    "review": {"available": True},
                    "finding_counts": {},
                    "comparison": {},
                },
                "critic_panel": {"all_required_roles": True},
            },
        )
        assert summary["eligible_positive_win"] is False


def test_human_skim_budget_removes_noncanonical_project_expansion(monkeypatch):
    catalog = _fixture_catalog()
    extra_id = "project:item4"
    catalog["entries"][extra_id] = {
        "id": extra_id,
        "kind": "project",
        "heading": "\\textbf{Project 4} | \\emph{Python}",
        "bullets": [
            {"id": extra_id + ":b1", "text": "\\textbf{Built a distinct project} with evidence"},
            {"id": extra_id + ":b2", "text": "\\textbf{Improved project workflow} across systems"},
        ],
    }
    monkeypatch.setattr(
        rs,
        "canonical_resume_benchmark",
        lambda _catalog: {"projects": [{"source_id": "project:item%s" % index} for index in range(4)]},
    )
    plan = _fixture_plan()
    plan["projects"].append({
        "source_id": extra_id,
        "bullets": [{"source_id": extra_id + ":b1"}, {"source_id": extra_id + ":b2"}],
        "why": "extra target evidence",
    })
    plan, errors = rs.validate_plan(plan, catalog, enhance=False)
    assert not errors

    curated = rs.curate_candidate_portfolio(plan, catalog)

    assert extra_id not in {item["source_id"] for item in curated["projects"]}
    assert any(
        action["kind"] == "project_roster_cap"
        for action in curated["portfolio_budget"]["actions"]
    )
    assert rs.portfolio_metrics(curated)["human_skim_budget"]["within_budget"] is True


def test_production_bridge_accepts_only_bounded_public_job_snapshots():
    value = rs.bridged_job({
        "id": "prod-123",
        "company": "Example AI",
        "title": "Machine Learning Engineer",
        "url": "https://jobs.example.com/roles/123",
        "locations": ["New York, NY"],
        "score": 92,
        "description": "Build production ML systems.",
        "alert_ok": True,
    })
    assert value is not None
    assert value["id"] == "prod-123"
    assert value["source"] == "job-radar-production-bridge"
    assert value["description"] == "Build production ML systems."
    assert rs.bridged_job({"id": "x", "company": "A", "title": "B", "url": "javascript:alert(1)"}) is None
    assert rs.bridged_job({"id": "x", "company": "A", "title": "B"}) is None


def test_resume_studio_ui_consumes_production_job_fragment_privately():
    assert "readBridgedJob" in rs.UI_HTML
    assert "job_snapshot:selected._bridged?selected:null" in rs.UI_HTML
    assert "opened from production" in rs.UI_HTML
    assert "JOB RADAR · PRIVATE RESUME WORKSPACE" in rs.UI_HTML
    assert "https://job-radar-newgrad.vercel.app" in rs.UI_HTML


def test_resume_studio_exposes_a_canonical_lock_and_private_render_boundary(tmp_path):
    lock = rs.canonical_resume_lock(tmp_path)
    assert lock["locked"] is True
    assert {item["name"] for item in lock["files"]} == {
        "CV/immutable/VictorJimenezResume.tex",
        "CV/immutable/VictorJimenezResume.pdf",
        "CV/immutable/og_resume.tex",
        "CV/immutable/og_resume.pdf",
        "CV/immutable/tldp_resume.tex",
        "CV/immutable/tldp_resume.pdf",
    }
    private = tmp_path / "CV" / ".resume_studio" / "runs" / "0123456789ab"
    rs.assert_resume_workspace(private, tmp_path)
    with pytest.raises(RuntimeError, match="canonical resume files are locked"):
        rs.assert_resume_workspace(tmp_path / "CV", tmp_path)
    assert "Canonical resumes locked" in rs.UI_HTML
    assert "CV/immutable/VictorJimenezResume.tex locked" in rs.UI_HTML
    assert "resume_lock.py unlock" in rs.UI_HTML
    assert "company_resume_ai.pdf" in rs.UI_HTML
    assert "Take-the-wheel" in rs.UI_HTML
    assert "How the modes differ" in rs.UI_HTML
    assert "Raw review data" in rs.UI_HTML
    assert "Evidence review" in rs.UI_HTML
    assert "/api/evidence/review" in rs.UI_HTML


def test_owner_resume_lock_uses_read_only_files_and_pin_gate(tmp_path, monkeypatch):
    cv_root = tmp_path / "CV"
    immutable = cv_root / "immutable"
    immutable.mkdir(parents=True)
    for relative in resume_lock.PROTECTED_RELATIVE_PATHS:
        path = cv_root / relative
        path.write_text("protected")

    status = resume_lock.lock_files(cv_root)
    assert status["locked"] is True
    assert all(not path.stat().st_mode & 0o200 for path in immutable.iterdir())

    monkeypatch.setattr(resume_lock, "_verify_pin", lambda pin: pin == "accepted")
    with pytest.raises(PermissionError):
        resume_lock.unlock_files("wrong", cv_root)
    unlocked = resume_lock.unlock_files("accepted", cv_root)
    assert unlocked["locked"] is False
    assert all(path.stat().st_mode & 0o200 for path in immutable.iterdir())


def test_resume_studio_exposes_a_canonical_lock_and_private_render_boundary(tmp_path):
    lock = rs.canonical_resume_lock(tmp_path)
    assert lock["locked"] is True
    assert {item["name"] for item in lock["files"]} == {
        "CV/immutable/VictorJimenezResume.tex",
        "CV/immutable/VictorJimenezResume.pdf",
        "CV/immutable/og_resume.tex",
        "CV/immutable/og_resume.pdf",
        "CV/immutable/tldp_resume.tex",
        "CV/immutable/tldp_resume.pdf",
    }
    private = tmp_path / "CV" / ".resume_studio" / "runs" / "0123456789ab"
    rs.assert_resume_workspace(private, tmp_path)
    with pytest.raises(RuntimeError, match="canonical resume files are locked"):
        rs.assert_resume_workspace(tmp_path / "CV", tmp_path)
    assert "Canonical resumes locked" in rs.UI_HTML
    assert "CV/immutable/VictorJimenezResume.tex locked" in rs.UI_HTML
    assert "resume_lock.py unlock" in rs.UI_HTML
    assert "company_resume_ai.pdf" in rs.UI_HTML
    assert "Take-the-wheel (moderate)" in rs.UI_HTML
    assert "Unchained generation" in rs.UI_HTML
    assert "Raw review data" in rs.UI_HTML
    assert "Evidence review" in rs.UI_HTML
    assert "/api/evidence/review" in rs.UI_HTML


def test_owner_resume_lock_uses_read_only_files_and_pin_gate(tmp_path, monkeypatch):
    cv_root = tmp_path / "CV"
    immutable = cv_root / "immutable"
    immutable.mkdir(parents=True)
    for relative in resume_lock.PROTECTED_RELATIVE_PATHS:
        path = cv_root / relative
        path.write_text("protected")

    status = resume_lock.lock_files(cv_root)
    assert status["locked"] is True
    assert all(not path.stat().st_mode & 0o200 for path in immutable.iterdir())

    monkeypatch.setattr(resume_lock, "_verify_pin", lambda pin: pin == "accepted")
    with pytest.raises(PermissionError):
        resume_lock.unlock_files("wrong", cv_root)
    unlocked = resume_lock.unlock_files("accepted", cv_root)
    assert unlocked["locked"] is False
    assert all(path.stat().st_mode & 0o200 for path in immutable.iterdir())


def test_resume_library_keeps_runs_and_legacy_experiments_with_posting_snapshots(tmp_path):
    run = tmp_path / "CV" / ".resume_studio" / "runs" / "0123456789ab"
    run.mkdir(parents=True)
    job = {"id": "job-1", "company": "Example Co", "title": "Data Scientist", "url": "https://example.test/job"}
    rs.write_json(run / "job.json", job)
    rs.write_json(run / "status.json", {
        "run_id": "0123456789ab", "mode": "dream", "status": "complete",
        "created_at": "2026-08-07T12:00:00+00:00", "pdf_filename": "example_co_resume_ai.pdf",
        "job": rs.job_summary(job),
    })
    rs.write_json(run / "job_context.json", {"posting_text": "Required: Python and SQL."})
    rs.write_json(run / "report.json", {
        "mode": "enhanced", "job": rs.job_summary(job), "review": {"craft_score": 88},
        "content_changes": {"keyword_coverage": {
            "posting_available": True, "detected_count": 2, "supported_count": 1,
            "covered_count": 1, "supported_exact_coverage_percent": 100,
            "exact_coverage_percent": 50,
            "terms": [
                {"term": "Python", "supported": True, "rendered": True, "status": "covered", "source_ids": ["experience:x:b1"]},
                {"term": "Kubernetes", "supported": False, "rendered": False, "status": "unsupported"},
            ],
        }},
        "review_overlay": {"available": True, "boxes": [{
            "left_percent": 10, "top_percent": 20, "width_percent": 70, "height_percent": 2,
            "terms": ["Python"], "text": "Built Python services", "kind": "ats",
        }]},
    })
    (run / "example_co_resume_ai.pdf").write_bytes(b"pdf")

    legacy = tmp_path / "CV" / ".resume_studio" / "architecture_experiments" / "old-example"
    legacy.mkdir(parents=True)
    legacy_job = {"id": "job-2", "company": "Old Co", "title": "Engineer"}
    rs.write_json(legacy / "report.json", {"mode": "enhanced", "job": rs.job_summary(legacy_job)})
    (legacy / "resume.pdf").write_bytes(b"legacy")

    entries = rs.resume_library(tmp_path)
    assert {entry["source"] for entry in entries} == {"run", "experiment"}
    saved = next(entry for entry in entries if entry["source"] == "run")
    assert saved["pdf_filename"] == "example_co_resume_ai.pdf"
    assert saved["has_posting_snapshot"] is True
    assert saved["objective"]["version"] == "objective-resume-v1"
    assert saved["objective"]["rankable"] is False
    assert saved["keyword_audit"]["supported_coverage_percent"] == 100
    assert saved["keyword_audit"]["terms"][1]["status"] == "unsupported"
    assert saved["keyword_audit"]["overlay"]["boxes"][0]["text"] == "Built Python services"
    snapshot = rs.posting_snapshot(tmp_path, "run", "0123456789ab")
    assert snapshot["posting_text"] == "Required: Python and SQL."


def test_objective_resume_assessment_is_target_specific_and_explains_unknown_reviewer(tmp_path):
    report = {
        "resume_match": {"score": 92, "missing_requirements": ["Kubernetes"]},
        "validation_warnings": ["one warning"],
        "review": {
            "unsupported_claims": ["Kubernetes"],
            "independent_review": False,
            "deterministic": {
                "gates": {"factual": {"status": "pass"}},
                "layout": {"pass": True},
                "portfolio": {"pass": True},
            },
        },
    }
    assessment = rs.objective_resume_assessment(report, "awaiting_review")

    assert assessment["version"] == "objective-resume-v1"
    assert assessment["rankable"] is True
    assert assessment["score"] < 92
    assert assessment["confidence"] == "low"
    assert any("No critic-panel result" in item for item in assessment["risks"])
    assert any(item["name"] == "Target fit" and item["source"] == "resume_match.score" for item in assessment["breakdown"])
    failed = rs.objective_resume_assessment(report, "failed")
    assert failed["score"] is None and failed["rankable"] is False


def test_objective_resume_assessment_excludes_a_draft_when_audit_prefers_base():
    report = {
        "resume_match": {"score": 94},
        "review": {
            "independent_review": False,
            "deterministic": {
                "gates": {"factual": {"status": "pass"}},
                "layout": {"pass": True},
                "portfolio": {"pass": True},
            },
        },
        "tailoring_audit": {
            "readiness": "review", "recommended_version": "base",
            "decision": "prefer_base",
        },
    }
    assessment = rs.objective_resume_assessment(report, "awaiting_review")
    assert assessment["rankable"] is False
    assert assessment["score"] is None
    assert assessment["recommended_version"] == "base"
    assert any("canonical base" in item for item in assessment["risks"])


def test_run_manager_snapshots_job_and_assigns_named_pdf(tmp_path):
    class NoopExecutor:
        def submit(self, *args, **kwargs):
            return None

    manager = rs.RunManager(tmp_path)
    manager.executor.shutdown(wait=False)
    manager.executor = NoopExecutor()
    job = {"id": "job-3", "company": "Acme Labs", "title": "ML Engineer"}
    status = manager.start(job, "dream", queue_id="queue-123")
    run_dir = tmp_path / "CV" / ".resume_studio" / "runs" / status["run_id"]
    assert status["pdf_filename"] == "acme_labs_resume_ai.pdf"
    assert status["queue_id"] == "queue-123"
    assert json.loads((run_dir / "job.json").read_text())["company"] == "Acme Labs"


def test_resume_library_preserves_queue_status_and_exposes_staleness(tmp_path):
    run_id = "0123456789ab"
    run_dir = tmp_path / "CV" / ".resume_studio" / "runs" / run_id
    run_dir.mkdir(parents=True)
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat()
    rs.write_json(run_dir / "job.json", {"id": "job-1", "company": "Acme Labs", "title": "ML Engineer"})
    rs.write_json(run_dir / "status.json", {
        "run_id": run_id,
        "mode": "generation",
        "status": "queued",
        "step": "queued",
        "message": "Queued",
        "created_at": old,
        "updated_at": old,
    })
    entry = rs.resume_library(tmp_path)[0]
    assert entry["status"] == "queued"
    assert entry["status"] != "interrupted"
    assert entry["stale"] is True
    assert "recoverable" in entry["stale_reason"]


def test_run_manager_recovers_queued_and_running_snapshots(tmp_path):
    class RecordingExecutor:
        def __init__(self):
            self.calls = []

        def submit(self, *args, **kwargs):
            self.calls.append(args)
            return None

        def shutdown(self, *args, **kwargs):
            return None

    runs = tmp_path / "CV" / ".resume_studio" / "runs"
    for run_id, status in (("0123456789ab", "queued"), ("abcdef012345", "running")):
        run_dir = runs / run_id
        run_dir.mkdir(parents=True)
        rs.write_json(run_dir / "job.json", {
            "id": "job-" + run_id,
            "company": "Acme Labs",
            "title": "ML Engineer",
        })
        rs.write_json(run_dir / "status.json", {
            "run_id": run_id, "mode": "generation", "status": status,
            "step": "drafting", "message": "Working",
        })
    complete_dir = runs / "fedcba543210"
    complete_dir.mkdir(parents=True)
    rs.write_json(complete_dir / "job.json", {"id": "job-done", "company": "Done", "title": "Engineer"})
    rs.write_json(complete_dir / "status.json", {"run_id": complete_dir.name, "mode": "generation", "status": "awaiting_review"})

    manager = rs.RunManager(tmp_path, max_workers=1)
    manager.executor.shutdown(wait=False)
    recorder = RecordingExecutor()
    manager.executor = recorder
    summary = manager.recover_pending()

    assert summary["recovered"] == 2
    assert summary["reset_running"] == 1
    assert len(recorder.calls) == 2
    recovered = json.loads((runs / "abcdef012345" / "status.json").read_text())
    assert recovered["status"] == "queued"
    assert recovered["recovered_from_status"] == "running"
    assert recovered["recovery_reason"] == "engine_restart"
    assert json.loads((complete_dir / "status.json").read_text())["status"] == "awaiting_review"
    manager.shutdown(wait=False)


def test_run_manager_repairs_shutdown_failure_on_next_start(tmp_path):
    class RecordingExecutor:
        def __init__(self):
            self.calls = []

        def submit(self, *args, **kwargs):
            self.calls.append(args)
            return None

        def shutdown(self, *args, **kwargs):
            return None

    run_id = "0123456789ab"
    run_dir = tmp_path / "CV" / ".resume_studio" / "runs" / run_id
    run_dir.mkdir(parents=True)
    rs.write_json(run_dir / "job.json", {"id": "job-1", "company": "Acme", "title": "Engineer"})
    rs.write_json(run_dir / "status.json", {
        "run_id": run_id, "mode": "generation", "status": "failed", "step": "error",
        "message": "cannot schedule new futures after interpreter shutdown",
    })
    manager = rs.RunManager(tmp_path, max_workers=1)
    manager.executor.shutdown(wait=False)
    recorder = RecordingExecutor()
    manager.executor = recorder
    summary = manager.recover_pending()
    assert summary["repaired_shutdown_failures"] == 1
    assert summary["recovered"] == 1
    saved = json.loads((run_dir / "status.json").read_text())
    assert saved["status"] == "queued"
    assert saved["recovered_from_status"] == "failed"
    assert saved["recovery_reason"] == "interpreter_shutdown_repair"
    manager.shutdown(wait=False)


def test_run_manager_marks_submitted_work_recoverable_during_shutdown(tmp_path):
    class NoopExecutor:
        def shutdown(self, *args, **kwargs):
            return None

    run_id = "0123456789ab"
    run_dir = tmp_path / "CV" / ".resume_studio" / "runs" / run_id
    run_dir.mkdir(parents=True)
    rs.write_json(run_dir / "status.json", {
        "run_id": run_id, "mode": "generation", "status": "running",
        "step": "drafting", "started_at": "old",
    })
    manager = rs.RunManager(tmp_path, max_workers=1)
    manager.executor.shutdown(wait=False)
    manager.executor = NoopExecutor()
    manager._submitted.add(run_id)
    manager.shutdown(wait=False)
    saved = json.loads((run_dir / "status.json").read_text())
    assert saved["status"] == "queued"
    assert saved["shutdown_requeue"] is True
    assert saved["recovery_reason"] == "engine_shutdown"


def test_run_manager_bounds_workers_and_rejects_stale_runtime(monkeypatch, tmp_path):
    assert rs.configured_run_workers(0) == 1
    assert rs.configured_run_workers(99) == rs.MAX_RUN_WORKERS
    assert rs.configured_run_workers("not-a-number") == rs.DEFAULT_RUN_WORKERS
    assert rs.configured_run_workers(3) == 3

    monkeypatch.setattr(rs, "_sha256_file", lambda path: "changed-on-disk")
    manager = rs.RunManager(tmp_path, max_workers=1)
    with pytest.raises(rs.ResumeStudioRuntimeStale, match="source changed"):
        manager.start({"id": "job-1", "company": "Acme", "title": "Engineer"}, "generation")
    assert manager.health()["workers"] == 1
    assert manager.health()["shutdown"] is False
    manager.shutdown(wait=False)


def test_run_manager_exposes_owner_checkpoint_instead_of_marking_draft_complete(monkeypatch, tmp_path):
    manager = rs.RunManager(tmp_path)
    manager.executor.shutdown(wait=False)

    def fake_dream(run_dir, job, update):
        update("awaiting_review", "critique ready")

    monkeypatch.setattr(rs, "run_dream", fake_dream)
    run_id = "0123456789ab"
    run_dir = tmp_path / "CV" / ".resume_studio" / "runs" / run_id
    run_dir.mkdir(parents=True)
    rs.write_json(run_dir / "status.json", {"run_id": run_id, "status": "queued"})
    manager._worker(run_id, run_dir, {"id": "job-1"}, "ai")
    assert json.loads((run_dir / "status.json").read_text())["status"] == "awaiting_review"


def test_approve_run_requires_ready_gates_and_records_owner_checkpoint(tmp_path):
    run_id = "0123456789ab"
    run_dir = tmp_path / "CV" / ".resume_studio" / "runs" / run_id
    run_dir.mkdir(parents=True)
    rs.write_json(run_dir / "status.json", {
        "run_id": run_id, "status": "awaiting_review", "step": "reviewing",
    })
    (run_dir / "google_resume_ai.pdf").write_bytes(b"%PDF-1.4\n")
    report = {"review": {"ready": True}, "approval_state": "awaiting_review"}
    rs.write_json(run_dir / "report.json", report)
    result = rs.approve_run(tmp_path, run_id)
    assert result["status"] == "complete"
    saved = json.loads((run_dir / "report.json").read_text())
    assert saved["approval_state"] == "approved"
    assert saved["approved_by"] == "Victor"


def test_approve_run_does_not_override_failed_quality_gates(tmp_path):
    run_id = "0123456789ab"
    run_dir = tmp_path / "CV" / ".resume_studio" / "runs" / run_id
    run_dir.mkdir(parents=True)
    rs.write_json(run_dir / "status.json", {"run_id": run_id, "status": "awaiting_review"})
    rs.write_json(run_dir / "report.json", {"review": {"ready": False}})
    with pytest.raises(ValueError, match="quality gates"):
        rs.approve_run(tmp_path, run_id)


def test_provider_plan_schema_cannot_return_a_latex_document():
    for enhance in (False, True):
        properties = rs.plan_schema(enhance)["properties"]
        assert "resume_tex" not in properties
        assert "experiences" in properties
        assert "projects" in properties


def test_plan_schema_requests_an_adaptive_ranked_candidate_pool():
    strict = rs.plan_schema(False)
    assert strict["properties"]["experiences"]["minItems"] == 0
    assert strict["properties"]["experiences"]["maxItems"] == rs.PORTFOLIO_CAPS["experiences"]["entries"]
    assert strict["properties"]["projects"]["minItems"] == 0
    assert strict["properties"]["projects"]["maxItems"] == rs.PORTFOLIO_CAPS["projects"]["entries"]
    assert strict["properties"]["leadership"]["minItems"] == 0
    assert strict["properties"]["leadership"]["maxItems"] == rs.PORTFOLIO_CAPS["leadership"]["entries"]
    strict_bullet = strict["properties"]["experiences"]["items"]["properties"]["bullets"]["items"]
    assert "priority" in strict_bullet["properties"]

    enhanced_bullet = rs.plan_schema(True)["properties"]["experiences"]["items"]["properties"]["bullets"]["items"]
    assert {"source_id", "text", "evidence_ids", "candidate_rationale"}.issubset(enhanced_bullet["required"])
    assert "source_ids" in enhanced_bullet["required"]
    assert "decision_ledger" in strict["properties"]
    assert "decision_ledger" in strict["required"]
    assert "front_matter_policy" in strict["properties"]
    assert "front_matter_policy" in strict["required"]

    generation = rs.plan_schema(True, generation=True)
    assert "front_matter_rewrites" in generation["required"]
    rewrite = generation["properties"]["front_matter_rewrites"]["items"]
    assert set(rewrite["required"]) == {"line_id", "text", "evidence_ids", "why"}
    assert rewrite["properties"]["line_id"]["enum"] == [
        "front:skills:0", "front:skills:1", "front:skills:2",
        "front:skills:3", "front:skills:4",
    ]


def test_gap_analysis_schema_is_requirement_complete_and_cannot_return_resume_copy():
    schema = rs.gap_analysis_schema()
    assert set(schema["required"]) == {
        "portfolio_strategy", "requirements", "must_cover_terms", "honest_gaps",
    }
    assert "resume_tex" not in schema["properties"]
    assert schema["properties"]["requirements"]["minItems"] == 8
    requirement = schema["properties"]["requirements"]["items"]
    assert requirement["properties"]["exact_terms"]["minItems"] == 1
    assert {"evidence_status", "evidence_ids", "recommended_action", "exact_terms"}.issubset(
        requirement["required"]
    )


def test_space_expansion_schema_is_source_addressed_and_cannot_return_a_new_section():
    schema = rs.space_expansion_schema()
    assert set(schema["required"]) == {"additions", "decision"}
    addition = schema["properties"]["additions"]["items"]
    assert set(addition["required"]) >= {
        "entry_id", "placement", "source_id", "source_ids", "evidence_ids", "text", "priority", "target_signal", "why",
    }
    assert "experiences" not in schema["properties"]
    assert addition["properties"]["source_ids"]["minItems"] == 1


def test_experience_order_is_canonical_even_when_portfolio_priority_arrives_reversed():
    catalog = _fixture_catalog()
    plan, errors = rs.validate_plan(_fixture_plan(), catalog, enhance=False)
    assert not errors
    plan["experiences"].reverse()
    ordered = rs.enforce_experience_order(plan, catalog)
    assert [entry["source_id"] for entry in ordered["experiences"]] == [
        "experience:item0", "experience:item1", "experience:item2",
    ]


def test_space_expansion_accepts_only_unused_authorized_meaningful_source_lines():
    catalog = _fixture_catalog()
    plan, errors = rs.validate_plan(_fixture_plan(), catalog, enhance=False)
    assert not errors
    plan["experiences"][0]["bullets"].pop()
    graph = {
        "nodes": [
            {"id": bullet["id"], "claim_allowed": True}
            for entry in catalog["entries"].values()
            for bullet in entry["bullets"]
        ]
    }
    data = {
        "additions": [
            {
                "entry_id": "experience:item0",
                "source_id": "experience:item0:b3",
                "source_ids": ["experience:item0:b3"],
                "evidence_ids": ["experience:item0:b3"],
                "text": catalog["entries"]["experience:item0"]["bullets"][2]["text"],
                "priority": 90,
                "target_signal": "communication",
                "why": "Adds a distinct presentation signal.",
            },
            {
                "entry_id": "experience:item0",
                "source_id": "experience:item0:b2",
                "source_ids": ["experience:item0:b2"],
                "evidence_ids": ["experience:item0:b2"],
                "text": "short",
                "priority": 80,
                "target_signal": "filler",
                "why": "Uses space.",
            },
        ],
        "decision": "Use only distinct evidence.",
    }
    additions, errors = rs._validate_space_additions(data, plan, catalog, graph)
    assert [item["source_id"] for item in additions] == ["experience:item0:b3"]
    assert any("repeats selected bullet" in error for error in errors)


def test_space_expansion_can_propose_a_unique_unused_project_as_a_trial_entry():
    catalog = _fixture_catalog()
    catalog["entries"]["project:item0"]["bullets"][2]["text"] = "Built a distinct project workflow with measurable technical depth"
    plan, errors = rs.validate_plan(_fixture_plan(), catalog, enhance=False)
    assert not errors
    plan["projects"] = [entry for entry in plan["projects"] if entry["source_id"] != "project:item0"]
    graph = {
        "nodes": [
            {"id": bullet["id"], "claim_allowed": True}
            for entry in catalog["entries"].values()
            for bullet in entry["bullets"]
        ]
    }
    data = {
        "additions": [{
            "entry_id": "project:item0",
            "placement": "new_entry",
            "source_id": "project:item0:b3",
            "source_ids": ["project:item0:b3"],
            "evidence_ids": ["project:item0:b3"],
            "text": catalog["entries"]["project:item0"]["bullets"][2]["text"],
            "priority": 95,
            "target_signal": "technical breadth",
            "why": "Adds a distinct project capability if the heading cost fits.",
        }],
        "decision": "Trial the distinct project.",
    }
    additions, errors = rs._validate_space_additions(data, plan, catalog, graph)
    assert not errors
    trial = rs._append_space_addition(plan, additions[0])
    assert any(entry["source_id"] == "project:item0" for entry in trial["projects"])
    assert trial["projects"][-1]["bullets"][0]["source_id"] == "project:item0:b3"


def test_measured_space_available_reads_legacy_capacity_reports():
    assert rs.measured_space_available({"vertical_capacity": {"qa_pages": 1, "warning": "one more bullet still fits"}})
    assert not rs.measured_space_available({"vertical_capacity": {"qa_pages": 2, "warning": "one more bullet overflows"}})


def test_new_entry_space_trial_requires_two_bullets():
    catalog = _fixture_catalog()
    plan, errors = rs.validate_plan(_fixture_plan(), catalog, enhance=False)
    assert not errors
    plan["projects"] = [entry for entry in plan["projects"] if entry["source_id"] != "project:item0"]
    graph = {"nodes": [
        {"id": bullet["id"], "claim_allowed": True}
        for entry in catalog["entries"].values()
        for bullet in entry["bullets"]
    ]}
    addition = {
        "entry_id": "project:item0", "placement": "new_entry", "section": "projects",
        "source_id": "project:item0:b1", "source_ids": ["project:item0:b1"],
        "evidence_ids": ["project:item0:b1"], "text": catalog["entries"]["project:item0"]["bullets"][0]["text"],
        "priority": 90, "target_signal": "breadth", "why": "distinct project",
    }
    with __import__("tempfile").TemporaryDirectory() as directory:
        _, result = rs.expand_into_measured_space(plan, [addition], catalog, graph, Path(directory))
    assert not result["applied"]
    assert "requires at least two bullets" in result["rejected"][0]["reason"]


def test_deterministic_space_fallback_prefers_distinct_selected_entries():
    catalog = _fixture_catalog()
    plan, errors = rs.validate_plan(_fixture_plan(), catalog, enhance=False)
    assert not errors
    plan["experiences"][0]["bullets"].pop()
    graph = {"nodes": [
        {"id": bullet["id"], "claim_allowed": True}
        for entry in catalog["entries"].values()
        for bullet in entry["bullets"]
    ]}
    additions = rs.deterministic_space_additions(plan, catalog, graph=graph, keyword_strategy={})
    assert additions
    assert additions[0]["placement"] == "append_bullet"
    assert additions[0]["entry_id"] == "experience:item0"


def test_space_swap_candidates_never_trim_core_experience():
    catalog = _fixture_catalog()
    plan, errors = rs.validate_plan(_fixture_plan(), catalog, enhance=False)
    assert not errors
    candidates = rs._space_removal_candidates(plan)
    assert candidates
    assert all(item[1] in {"projects", "leadership"} for item in candidates)
    trimmed = rs._apply_space_removals(plan, candidates[:2])
    assert sum(len(entry["bullets"]) for entry in trimmed["experiences"]) == sum(
        len(entry["bullets"]) for entry in plan["experiences"]
    )


def test_space_expansion_single_swap_trial_keeps_action_tuple_shape(monkeypatch, tmp_path):
    catalog = _fixture_catalog()
    plan, errors = rs.validate_plan(_fixture_plan(), catalog, enhance=False)
    assert not errors
    graph = {"nodes": [
        {"id": bullet["id"], "claim_allowed": True}
        for entry in catalog["entries"].values()
        for bullet in entry["bullets"]
    ]}
    source = catalog["entries"]["project:item0"]["bullets"][2]
    addition = {
        "entry_id": "project:item0",
        "section": "projects",
        "placement": "append_bullet",
        "source_id": source["id"],
        "source_ids": [source["id"]],
        "evidence_ids": [source["id"]],
        "text": source["text"],
        "priority": 95,
        "target_signal": "communication",
        "why": "Adds a distinct verified signal when a lower-value project line is displaced.",
    }
    original_count = sum(
        len(entry["bullets"])
        for section in ("experiences", "projects", "leadership")
        for entry in plan[section]
    )

    def fake_pack(candidate, _catalog, _run_dir):
        count = sum(
            len(entry["bullets"])
            for section in ("experiences", "projects", "leadership")
            for entry in candidate[section]
        )
        if count > original_count:
            raise RuntimeError("forced direct-trial overflow")
        return candidate, {"compiled": True, "pages": 1, "overfull": False}

    monkeypatch.setattr(rs, "pack_plan_to_page", fake_pack)
    expanded, result = rs.expand_into_measured_space(
        plan, [addition], catalog, graph, tmp_path,
    )
    assert result["applied"]
    assert source["id"] in rs._selected_bullet_ids(expanded)
    assert result["replaced"]


def test_line_compaction_shortens_safe_connective_phrases():
    rag = "Unified RAG infrastructure on Google Cloud with AlloyDB/pgvector for embeddings, vector search, and SQL retrieval"
    pytorch = "Extended modular PyTorch framework with 3+ arithmetic features, enabling repeatable linear-algebra experiments"
    rag_candidates = rs._line_compaction_candidates(rag, rag)
    pytorch_candidates = rs._line_compaction_candidates(pytorch, pytorch)
    assert "Unified RAG infrastructure on Google Cloud with AlloyDB/pgvector for embeddings, vector search, SQL retrieval" in rag_candidates
    assert "Extended modular PyTorch framework with 3+ arithmetic features for repeatable linear-algebra experiments" in pytorch_candidates
    nba = "Trained and tuned three classification models with resampling and stratified validation to handle rare All-NBA selections"
    assert "Trained and tuned three classifiers with resampling and stratified validation for rare All-NBA selections" in rs._line_compaction_candidates(nba, nba)
    metric = "Reached 0.984 ROC-AUC and 94% recall while handling a 2.74% positive class with resampling and stratified validation"
    assert "Reached 0.984 ROC-AUC and 94% recall on a 2.74% positive class using resampling and stratified validation" in rs._line_compaction_candidates(metric, metric)
    skills = "Data & Tools: Data Engineering, Data Visualization, SQLite, Vector Databases, Git, GitHub, SharePoint, Testing, Debugging, Version Control"
    candidates = rs._line_compaction_candidates(skills, skills)
    assert "Data & Tools: Data Engineering, Data Visualization, SQLite, Vector Databases, Git/GitHub, SharePoint, Testing, Debugging, Version Control" in candidates
    assert "Data & Tools: Data Engineering, Data Visualization, SQLite, Git, GitHub, SharePoint, Testing, Debugging, Version Control" in candidates
    ai_skills = "AI/ML: Machine Learning, LLMs, RAG, Agentic AI, Gemini, PyTorch, Computer Vision, LightRAG, FastMCP"
    ai_candidates = rs._line_compaction_candidates(ai_skills, ai_skills)
    assert "AI/ML: ML, LLMs, RAG, Agentic AI, Gemini, PyTorch, Computer Vision, LightRAG, FastMCP" in ai_candidates
    assert "AI/ML: Machine Learning, LLMs, RAG, Agentic AI, Gemini, PyTorch, CV, LightRAG, FastMCP" in ai_candidates
    fallback = "Kept live conversations responsive with asynchronous emotion analysis, returning a safe fallback when processing lagged"
    fallback_candidates = rs._line_compaction_candidates(fallback, fallback)
    assert "Kept conversations responsive via asynchronous emotion analysis, with safe fallback on lag" in fallback_candidates
    expanded = "Engineered adaptive ML/DL posture pipeline from live sensor data, delivering real-time posture classification and slouch alerts"
    source = "Engineered adaptive ML/DL posture pipeline from live sensor data, delivering real-time slouch alerts"
    assert source in rs._line_compaction_candidates(expanded, source)


def test_enhanced_plan_can_synthesize_multiple_authorized_source_lines():
    catalog = _fixture_catalog()
    plan, errors = rs.validate_plan(_fixture_plan(), catalog, enhance=False)
    assert not errors
    graph = {
        "nodes": [
            {"id": bullet["id"], "claim_allowed": True}
            for entry in catalog["entries"].values()
            for bullet in entry["bullets"]
        ]
    }
    enhanced = json.loads(json.dumps(plan))
    bullet = enhanced["experiences"][0]["bullets"][0]
    bullet.update({
        "text": "\\textbf{Architected a stronger item} combining workflow and evidence",
        "source_ids": [
            "experience:item0:b1",
            "experience:item0:b2",
        ],
        "evidence_ids": [
            "experience:item0:b1",
            "experience:item0:b2",
        ],
        "candidate_rationale": "Synthesis keeps both independently authorized mechanisms.",
    })
    normalized, errors = rs.validate_plan(enhanced, catalog, enhance=True, graph=graph)
    assert not errors
    assert normalized["experiences"][0]["bullets"][0]["source_ids"] == [
        "experience:item0:b1",
        "experience:item0:b2",
    ]


def test_validation_merges_distinct_bullets_from_duplicate_entry():
    catalog = _fixture_catalog()
    plan = _fixture_plan()
    duplicate = json.loads(json.dumps(plan["leadership"][0]))
    duplicate["bullets"] = [{"source_id": "leadership:item0:b2"}]
    duplicate["why"] = "customer-facing conflict resolution"
    plan["leadership"].append(duplicate)
    normalized, errors = rs.validate_plan(plan, catalog, enhance=False)
    assert not errors
    assert len(normalized["leadership"]) == 1
    assert [item["source_id"] for item in normalized["leadership"][0]["bullets"]] == [
        "leadership:item0:b1",
        "leadership:item0:b2",
    ]
    assert normalized["validation_warnings"] == [
        "merged duplicate entry: leadership:item0"
    ]


def test_review_schema_is_critique_only_and_does_not_return_a_replacement_plan():
    schema = rs.reviewed_plan_schema(True)
    assert "final_plan" not in schema["properties"]
    assert {"blocking_issues", "line_feedback", "unsupported_claims"}.issubset(schema["required"])
    assert "decision_feedback" in schema["required"]
    assert "portfolio_comparison" in schema["required"]
    assert set(rs.REVIEW_CRITERIA).issubset(schema["properties"]["criteria"]["required"])


def _fixture_catalog():
    entries = {}
    for kind, count in (("experience", 3), ("project", 4), ("leadership", 1)):
        for index in range(count):
            entry_id = "%s:item%s" % (kind, index)
            entry = {
                "id": entry_id,
                "kind": kind,
                "bullets": [
                    {"id": "%s:b1" % entry_id, "text": "\\textbf{Built %s item %s} with evidence" % (kind, index)},
                    {"id": "%s:b2" % entry_id, "text": "\\textbf{Improved %s workflow} across %s systems" % (kind, index + 10)},
                    {"id": "%s:b3" % entry_id, "text": "\\textbf{Presented %s findings} to %s stakeholders" % (kind, index + 20)},
                ],
            }
            if kind == "project":
                entry["heading"] = "\\textbf{Project %s} | \\emph{Python}" % index
            else:
                entry.update(
                    {
                        "company": "Company %s" % index,
                        "role": "Role %s" % index,
                        "dates": "2025 -- 2026",
                        "location": "Newark, NJ",
                    }
                )
            entries[entry_id] = entry
    return {"template": "resume.tex", "entries": entries}


def _fixture_plan():
    def selected(kind, count, bullets):
        return [
            {
                "source_id": "%s:item%s" % (kind, index),
                "bullets": [{"source_id": "%s:item%s:b%s" % (kind, index, bullet)} for bullet in range(1, bullets + 1)],
                "why": "target evidence",
            }
            for index in range(count)
        ]

    return {
        "positioning_thesis": "Targeted engineer",
        "selected_evidence": [],
        "excluded_evidence": [],
        "experiences": selected("experience", 3, 3),
        "projects": selected("project", 4, 2),
        "leadership": selected("leadership", 1, 1),
        "revision_notes": [],
    }


def test_source_only_plan_uses_catalog_text_and_rejects_layout_commands():
    catalog = _fixture_catalog()
    plan, errors = rs.validate_plan(_fixture_plan(), catalog, enhance=False)
    assert not errors
    assert plan["experiences"][0]["bullets"][0]["text"].startswith("\\textbf{Built")

    enhanced = _fixture_plan()
    for section in ("experiences", "projects", "leadership"):
        for entry in enhanced[section]:
            for bullet in entry["bullets"]:
                bullet["text"] = "\\vspace{-20pt} cheat"
    _, errors = rs.validate_plan(enhanced, catalog, enhance=True)
    assert any("forbidden layout command" in error for error in errors)


def test_project_swap_requires_every_omitted_canonical_bullet_source_id():
    catalog = _fixture_catalog()
    for bullet in catalog["entries"]["project:item0"]["bullets"]:
        bullet["source"] = "resume.tex"
    plan = _fixture_plan()
    plan["projects"] = [
        entry for entry in plan["projects"]
        if entry["source_id"] != "project:item0"
    ]
    plan["decision_ledger"] = [{"source_ids": ["project:item0:b1", "project:item0:b2"]}]
    errors = rs._project_tradeoff_source_errors(plan, catalog)
    assert any("project:item0:b3" in error for error in errors)
    plan["decision_ledger"][0]["source_ids"].append("project:item0:b3")
    assert rs._project_tradeoff_source_errors(plan, catalog) == []


def test_validate_plan_fails_closed_on_incomplete_project_tradeoff_ledger():
    catalog = _fixture_catalog()
    for bullet in catalog["entries"]["project:item0"]["bullets"]:
        bullet["source"] = "resume.tex"
    plan = _fixture_plan()
    plan["projects"] = [
        entry for entry in plan["projects"]
        if entry["source_id"] != "project:item0"
    ]
    for section in ("experiences", "projects", "leadership"):
        for entry in plan[section]:
            for bullet in entry["bullets"]:
                bullet.update({
                    "text": catalog["entries"][entry["source_id"]]["bullets"][
                        int(bullet["source_id"].rsplit(":b", 1)[1]) - 1
                    ]["text"],
                    "source_ids": [bullet["source_id"]],
                    "evidence_ids": [bullet["source_id"]],
                    "candidate_rationale": "source-grounded selection",
                })
    plan["decision_ledger"] = [{"source_ids": ["project:item0:b1", "project:item0:b2"]}]
    graph = {"nodes": [
        {"id": bullet["id"], "claim_allowed": True}
        for entry in catalog["entries"].values()
        for bullet in entry["bullets"]
    ]}
    _, errors = rs.validate_plan(plan, catalog, enhance=True, graph=graph)
    assert any("project:item0:b3" in error for error in errors)


def test_enhancement_that_drops_scope_qualifier_reverts_to_source():
    catalog = _fixture_catalog()
    source = catalog["entries"]["experience:item0"]["bullets"][0]
    source["text"] = "Built a synthetic-data prototype for evaluation"
    plan, errors = rs.validate_plan(_fixture_plan(), catalog, enhance=False)
    assert not errors
    plan["experiences"][0]["bullets"][0].update(
        {
            "text": "Built a data system for evaluation",
            "evidence_ids": [source["id"]],
            "candidate_rationale": "shorter",
        }
    )
    normalized, errors = rs.validate_plan(plan, catalog, enhance=True)
    assert not errors
    assert normalized["experiences"][0]["bullets"][0]["text"] == source["text"]
    assert any(
        "reverted enhanced bullet experience:item0:b1 after it dropped protected qualifier(s): prototype, synthetic"
        in warning
        for warning in normalized["validation_warnings"]
    )


def test_poc_and_proof_of_concept_are_equivalent_protected_qualifiers():
    assert not rs._missing_protected_qualifiers(
        "Architected a proof of concept for retrieval",
        "Architected a POC for retrieval",
    )


def test_model_math_command_is_normalized_and_other_inline_commands_fail():
    assert rs._normalize_model_fragment("Delivered 3\\times{} faster retrieval") == "Delivered 3x faster retrieval"
    assert rs._normalize_model_fragment("Delivered 3\\texttimes{} faster retrieval") == "Delivered 3x faster retrieval"
    assert rs._normalize_model_fragment("Received $8,000 and improved 94%") == "Received \\$8,000 and improved 94\\%"
    assert rs._normalize_model_fragment("A & B with a_b") == "A \\& B with a\\_b"
    assert rs._unsupported_inline_commands("\\textbf{Built} with \\emph{care}") == []
    assert rs._unsupported_inline_commands("Used \\sqrt{n}") == ["sqrt"]


def test_unknown_provider_bullet_is_dropped_with_warning_when_entry_has_valid_text():
    catalog = _fixture_catalog()
    plan, errors = rs.validate_plan(_fixture_plan(), catalog, enhance=False)
    assert not errors
    plan["experiences"][0]["bullets"].append({"source_id": "experience:item0:b999"})
    normalized, errors = rs.validate_plan(plan, catalog, enhance=False)
    assert not errors
    assert len(normalized["experiences"][0]["bullets"]) == 3
    assert "dropped unknown bullet experience:item0:b999 for experience:item0" in normalized["validation_warnings"]


def test_entry_level_bullet_source_is_recovered_from_exact_supporting_id():
    catalog = _fixture_catalog()
    plan, errors = rs.validate_plan(_fixture_plan(), catalog, enhance=False)
    assert not errors
    graph = {
        "nodes": [
            {"id": bullet["id"], "claim_allowed": True}
            for entry in catalog["entries"].values()
            for bullet in entry["bullets"]
        ]
    }
    enhanced = json.loads(json.dumps(plan))
    enhanced["experiences"][0]["bullets"][0].update({
        "source_id": "experience:item0",
        "source_ids": ["experience:item0:b1"],
        "evidence_ids": ["experience:item0:b1"],
        "text": "\\textbf{Built item} with evidence",
        "candidate_rationale": "provider used the parent entry ID",
    })
    normalized, errors = rs.validate_plan(enhanced, catalog, enhance=True, graph=graph)
    assert not errors
    assert normalized["experiences"][0]["bullets"][0]["source_id"] == "experience:item0:b1"
    assert any("normalized entry-level bullet source experience:item0" in warning for warning in normalized["validation_warnings"])


def test_unknown_enhanced_citation_is_dropped_when_authoritative_source_remains():
    catalog = _fixture_catalog()
    plan, errors = rs.validate_plan(_fixture_plan(), catalog, enhance=False)
    assert not errors
    graph = {
        "nodes": [
            {"id": bullet["id"], "claim_allowed": True}
            for entry in catalog["entries"].values()
            for bullet in entry["bullets"]
        ]
    }
    enhanced = json.loads(json.dumps(plan))
    enhanced["experiences"][0]["bullets"][0].update(
        {
            "text": "\\textbf{Built item} with evidence",
            "evidence_ids": ["experience:item0:b1", "doc:missing"],
            "candidate_rationale": "target evidence",
        }
    )
    normalized, errors = rs.validate_plan(enhanced, catalog, enhance=True, graph=graph)
    assert not errors
    assert normalized["experiences"][0]["bullets"][0]["evidence_ids"] == ["experience:item0:b1"]
    assert any("dropped unknown evidence" in warning for warning in normalized["validation_warnings"])


def test_candidate_expansion_builds_balanced_reference_sized_pool():
    catalog = _fixture_catalog()
    plan, errors = rs.validate_plan(_fixture_plan(), catalog, enhance=False)
    assert not errors
    expanded = rs.expand_candidate_portfolio(plan, catalog, enhance=True)
    assert rs.portfolio_metrics(expanded)["total_bullets"] == 18
    assert all(len(entry["bullets"]) == 2 for entry in expanded["projects"])


def test_candidate_expansion_adds_authorized_source_backups():
    catalog = _fixture_catalog()
    plan, errors = rs.validate_plan(_fixture_plan(), catalog, enhance=False)
    assert not errors
    plan["experiences"][0]["bullets"].pop()
    expanded = rs.expand_candidate_portfolio(plan, catalog, enhance=True)
    assert rs.portfolio_metrics(expanded)["total_bullets"] == 17
    added = expanded["experiences"][0]["bullets"][-1]
    assert added["source_id"].endswith(":b2")

    edited = json.loads(json.dumps(plan))
    edited["projects"][0]["bullets"][0]["text"] = "edited text"
    merged = rs.merge_edited_bullets(expanded, edited)
    assert merged["projects"][0]["bullets"][0]["text"] == "edited text"
    assert rs.portfolio_metrics(merged)["total_bullets"] == 17


def test_line_merge_cannot_delete_generated_skills_evidence():
    candidate = _fixture_plan()
    candidate["front_matter_rewrites"] = [{
        "line_id": "front:skills:3",
        "text": "Data & Tools: GitHub, SharePoint, Testing, Version Control",
        "evidence_ids": ["doc:skills"],
        "why": "Target evidence",
        "source_text": "Data & Tools: GitHub",
    }]
    edited = _fixture_plan()
    edited["front_matter_rewrites"] = []
    merged = rs.merge_edited_bullets(candidate, edited)
    assert merged["front_matter_rewrites"] == candidate["front_matter_rewrites"]

    edited["front_matter_rewrites"] = [{
        "line_id": "front:skills:3", "text": "Data & Tools: GitHub, SharePoint",
        "evidence_ids": [], "why": "", "source_text": "",
    }]
    merged = rs.merge_edited_bullets(candidate, edited)
    assert merged["front_matter_rewrites"][0]["text"].endswith("GitHub, SharePoint")
    assert merged["front_matter_rewrites"][0]["evidence_ids"] == ["doc:skills"]


def test_downstream_validation_preserves_existing_generation_skills(monkeypatch):
    catalog = _fixture_catalog()
    graph = {"nodes": [{
        "id": "doc:skills", "heading": "Skills", "text": "Used SharePoint",
        "claim_allowed": True,
    }]}
    plan = _fixture_plan()
    plan["front_matter_rewrites"] = [{
        "line_id": "front:skills:3",
        "text": "\\textbf{Data \\& Tools:} GitHub, SharePoint",
        "evidence_ids": ["doc:skills"],
        "why": "Target evidence",
    }]
    monkeypatch.setattr(rs, "front_matter_catalog", lambda _root: [{
        "line_id": "front:skills:3", "text": "\\textbf{Data \\& Tools:} GitHub",
    }])
    normalized, errors = rs.validate_plan(plan, catalog, enhance=False, graph=graph)
    assert not errors
    assert normalized["front_matter_rewrites"][0]["line_id"] == "front:skills:3"


def test_skills_validator_rejects_new_technology_without_matching_cited_evidence(monkeypatch):
    catalog = _fixture_catalog()
    graph = {"nodes": [{
        "id": "doc:skills", "heading": "Skills", "text": "Used GitHub",
        "claim_allowed": True,
    }]}
    plan = _fixture_plan()
    plan["front_matter_rewrites"] = [{
        "line_id": "front:skills:3",
        "text": "Data & Tools: GitHub, pytest",
        "evidence_ids": ["doc:skills"],
        "why": "Target testing term",
    }]
    monkeypatch.setattr(rs, "front_matter_catalog", lambda _root: [{
        "line_id": "front:skills:3", "text": "Data & Tools: GitHub",
    }])
    normalized, errors = rs.validate_plan(plan, catalog, enhance=False, graph=graph)
    assert not errors
    assert normalized["front_matter_rewrites"] == []
    assert any("unsupported introduced term" in warning for warning in normalized["validation_warnings"])


def test_workshop_edit_creates_revision_without_overwriting_original_plan(monkeypatch, tmp_path):
    catalog = _fixture_catalog()
    plan, errors = rs.validate_plan(_fixture_plan(), catalog, enhance=False)
    assert not errors
    run_id = "0123456789ab"
    run_dir = tmp_path / "CV" / ".resume_studio" / "runs" / run_id
    run_dir.mkdir(parents=True)
    rs.write_json(run_dir / "content_plan.json", plan)
    rs.write_json(run_dir / "job.json", {"id": "job-1", "company": "Example Co", "title": "ML Engineer"})
    rs.write_json(run_dir / "status.json", {"run_id": run_id, "mode": "dream", "status": "complete"})
    monkeypatch.setattr(rs, "source_catalog", lambda root=None: catalog)
    monkeypatch.setattr(
        rs,
        "_workshop_render",
        lambda *args, **kwargs: {
            "revision_id": args[2]["revision_id"],
            "pdf_filename": "example_workshop.pdf",
            "preview_filename": "example_workshop.png",
            "compiled": True,
            "layout": {"pages": 1},
        },
    )
    line_id = plan["experiences"][0]["bullets"][0]["source_id"]
    result = rs.workshop_apply_edit(
        tmp_path, run_id, line_id, "\\textbf{Built a better item} with evidence", origin="manual"
    )
    assert result["last_render"]["revision_id"]
    assert result["lines"][0]["text"] == "\\textbf{Built a better item} with evidence"
    assert json.loads((run_dir / "content_plan.json").read_text())["experiences"][0]["bullets"][0]["text"] != "\\textbf{Built a better item} with evidence"
    state = json.loads((run_dir / "workshop.json").read_text())
    assert len(state["revisions"]) == 1
    reverted = rs.workshop_revert(tmp_path, run_id, state["revisions"][0]["revision_id"])
    assert reverted["lines"][0]["text"] == "\\textbf{Built a better item} with evidence"
    assert len(json.loads((run_dir / "workshop.json").read_text())["revisions"]) == 2


@pytest.mark.skipif(
    not (rs.repo_root() / "CV" / rs.CANONICAL_TEMPLATE).is_file(),
    reason="CV/ is local-only; exact-template workshop test runs on the owner Mac",
)
def test_workshop_front_matter_is_editable_without_changing_the_template(tmp_path):
    front = rs.front_matter_catalog()
    assert {item["line_id"] for item in front} >= {
        "front:education:school",
        "front:skills:0",
        "front:skills:3",
    }
    catalog = _fixture_catalog()
    plan, errors = rs.validate_plan(_fixture_plan(), catalog, enhance=False)
    assert not errors
    plan["front_matter"] = json.loads(json.dumps(front))
    next(item for item in plan["front_matter"] if item["line_id"] == "front:education:school")["text"] = "New Jersey Institute of Technology"
    next(item for item in plan["front_matter"] if item["line_id"] == "front:skills:0")["text"] = "\\textbf{Languages:} Python, Rust"
    tex = rs.render_workshop_plan(plan, catalog, rs.repo_root())
    assert "New Jersey Institute of Technology" in tex
    assert "Python, Rust" in tex
    skills_section = "\\section{Technical Skills}" if "\\section{Technical Skills}" in tex else "\\section{Skills}"
    assert tex.index(skills_section) < tex.index("Python, Rust")
    assert tex.count("\\textbf{Languages:}") == 1
    assert tex.count("\\textbf{Data \\& Tools:}") == 1
    assert rs.template_style_guard(tex, rs.repo_root())["identical_preamble_header_education_skills"] is False


@pytest.mark.skipif(
    not (rs.repo_root() / "CV" / rs.CANONICAL_TEMPLATE).is_file(),
    reason="CV/ is local-only; exact-template workshop test runs on the owner Mac",
)
def test_workshop_plan_refreshes_stale_front_matter_indexes_without_losing_edits():
    plan = _fixture_plan()
    stale = rs.front_matter_catalog()
    languages = next(item for item in stale if item["line_id"] == "front:skills:0")
    languages["template_index"] = 1
    languages["text"] = "\\textbf{Languages:} Python, Rust"
    plan["front_matter"] = stale
    refreshed = rs._workshop_plan(plan, rs.repo_root())
    updated = next(item for item in refreshed["front_matter"] if item["line_id"] == "front:skills:0")
    canonical = next(item for item in rs.front_matter_catalog() if item["line_id"] == "front:skills:0")
    assert updated["template_index"] == canonical["template_index"]
    assert updated["text"] == "\\textbf{Languages:} Python, Rust"


def test_workshop_ai_returns_candidates_without_mutating_the_current_draft(monkeypatch, tmp_path):
    catalog = _fixture_catalog()
    plan, errors = rs.validate_plan(_fixture_plan(), catalog, enhance=False)
    assert not errors
    run_id = "0123456789ab"
    run_dir = tmp_path / "CV" / ".resume_studio" / "runs" / run_id
    run_dir.mkdir(parents=True)
    rs.write_json(run_dir / "content_plan.json", plan)
    rs.write_json(run_dir / "job.json", {"id": "job-1", "company": "Example Co", "title": "ML Engineer"})
    rs.write_json(run_dir / "status.json", {"run_id": run_id, "mode": "dream", "status": "complete"})
    monkeypatch.setattr(rs, "source_catalog", lambda root=None: catalog)
    monkeypatch.setattr(rs, "evidence_graph", lambda root=None: {"nodes": []})
    monkeypatch.setattr(rs, "provider_commands", lambda: {"codex": "/usr/bin/codex"})
    line_id = plan["experiences"][0]["bullets"][0]["source_id"]
    monkeypatch.setattr(
        rs,
        "run_provider",
        lambda *args, **kwargs: {
            "provider": "codex", "ok": True, "elapsed_seconds": 1.2,
            "usage_tokens": 123,
            "data": {
                "reply": "I kept the artifact and ownership visible.",
                "suggestions": [{
                    "line_id": line_id,
                    "text": "\\textbf{Architected the system} with a clearer ownership story",
                    "rationale": "Leads with the technical artifact and preserves the authorized scope.",
                    "evidence_ids": [line_id],
                }],
                "warnings": [],
            },
        },
    )
    result = rs.workshop_ai(tmp_path, run_id, "Make this line more decisive", line_id=line_id, provider="codex")
    assert result["suggestions"][0]["line_id"] == line_id
    assert result["usage_tokens"] == 123
    assert result["workshop"]["messages"][-1]["kind"] == "ai"
    assert json.loads((run_dir / "content_plan.json").read_text())["experiences"][0]["bullets"][0]["text"] != result["suggestions"][0]["text"]


def test_semantic_duplicate_with_abbreviated_unit_is_detected():
    assert rs._same_resume_bullet(
        "Engineered computer vision pipeline tracking 7 emotions every 2.4s from gaze and facial expressions",
        "Engineered a computer vision pipeline tracking 7 emotions every 2.4 seconds from gaze and facial expressions",
    )


def test_same_entry_repeated_metric_and_proof_is_detected():
    assert rs._same_entry_resume_bullet(
        "Translated 40+ clinician interviews into four hardware revisions",
        "Led market validation through 40+ interviews, shaping the product roadmap",
    )
    assert rs._same_entry_resume_bullet(
        "Engineered posture pipeline from 10,000+ calibration samples",
        "Designed calibration workflow collecting 10,000+ motion samples",
    )
    assert not rs._same_entry_resume_bullet(
        "Led a 3-person team building an AlloyDB foundation",
        "Unified 3 AI systems into one conversation timeline",
    )
    assert rs._same_entry_resume_bullet(
        "Reached 0.984 ROC-AUC and 94% recall using resampling and stratified validation",
        "Trained three classifiers with resampling and stratified validation for rare selections",
    )


def test_curator_removes_cross_entry_repeated_posture_story():
    plan = {
        "experiences": [{
            "source_id": "experience:posture",
            "bullets": [{
                "source_id": "experience:posture:b3",
                "text": "Designed a sub-minute calibration workflow capturing 10,000+ motion samples for adaptive model training",
                "priority": 90,
            }],
        }],
        "projects": [{
            "source_id": "project:posturemax",
            "bullets": [{
                "source_id": "project:posturemax:b2",
                "text": "Captured 10,000+ calibration samples to personalize live posture feedback",
                "priority": 70,
            }],
        }],
        "leadership": [],
    }

    curated = rs.curate_candidate_portfolio(plan)

    assert curated["projects"] == []
    assert any(
        action["source_id"] == "project:posturemax:b2"
        for action in curated["portfolio_budget"]["actions"]
    )


def test_near_copy_rewrite_is_rejected_as_low_value_churn():
    source = (
        "Architected an agentic LLM POC, leading a 3-person team to establish "
        "an extensible AlloyDB foundation for drug safety"
    )
    candidate = (
        "Architected an agentic LLM POC, leading a 3-person team to build "
        "an extensible AlloyDB drug-safety foundation"
    )
    assert rs._low_value_rewrite(source, candidate)
    assert not rs._low_value_rewrite(
        source,
        "Architected an agentic LLM POC, leading a 3-person team to build "
        "an extensible AlloyDB foundation with row-level access control for drug safety",
    )


def test_enhanced_near_copy_reverts_to_authorized_source_wording():
    catalog = _fixture_catalog()
    source = catalog["entries"]["experience:item0"]["bullets"][0]
    source["text"] = (
        "Architected an agentic LLM POC, leading a 3-person team to establish "
        "an extensible AlloyDB foundation for drug safety"
    )
    plan, errors = rs.validate_plan(_fixture_plan(), catalog, enhance=False)
    assert not errors
    bullet = plan["experiences"][0]["bullets"][0]
    bullet.update({
        "text": (
            "Architected an agentic LLM POC, leading a 3-person team to build "
            "an extensible AlloyDB drug-safety foundation"
        ),
        "source_ids": [source["id"]],
        "evidence_ids": [source["id"]],
    })
    graph = {"nodes": [
        {"id": item["id"], "claim_allowed": True}
        for entry in catalog["entries"].values()
        for item in entry["bullets"]
    ]}
    for section in ("experiences", "projects", "leadership"):
        for entry in plan[section]:
            for item in entry["bullets"]:
                item.setdefault("source_ids", [item["source_id"]])
                item.setdefault("evidence_ids", [item["source_id"]])
    normalized, errors = rs.validate_plan(plan, catalog, enhance=True, graph=graph)
    assert not errors
    assert normalized["experiences"][0]["bullets"][0]["text"] == source["text"]
    assert any("reverted low-value paraphrase" in warning for warning in normalized["validation_warnings"])


def test_review_preview_overlay_marks_ats_and_meaningful_change(monkeypatch, tmp_path):
    monkeypatch.setattr(
        rs,
        "pdf_line_geometry",
        lambda pdf: {
            "page_width": 612.0,
            "page_height": 792.0,
            "lines": [{
                "text": "Engineered Python backend with row-level access control",
                "x_min": 60.0,
                "x_max": 500.0,
                "y_min": 100.0,
                "y_max": 110.0,
            }],
        },
    )
    result = rs.review_preview_overlay(
        tmp_path / "draft.pdf",
        {"experiences": [{"bullets": [{"source_id": "experience:item0:b1", "text": "Engineered Python backend with row-level access control"}]}]},
        {"rewritten_bullets": [{"source_id": "experience:item0:b1"}], "added_bullets": [],
         "keyword_coverage": {"terms": [{"term": "Python", "supported": True, "rendered": True}]}},
        {"terms": [{"term": "Python", "supported": True, "rendered": True}]},
    )
    assert result["available"] is True
    assert result["boxes"][0]["kind"] == "both"
    assert result["boxes"][0]["text"].startswith("Engineered Python")


def test_target_priority_outweighs_generic_technical_tiebreakers():
    target_specific = {"priority": 90, "text": "Resolved biomedical version conflicts via Pandas/SQL"}
    generic = {"priority": 87, "text": "Engineered modular API cloud system architecture"}
    assert rs._bullet_value(target_specific) > rs._bullet_value(generic)


def test_quality_profiles_keep_deep_available_and_default_to_balanced():
    assert rs.normalize_quality_profile("") == "balanced"
    assert rs.normalize_quality_profile("unknown") == "balanced"
    assert rs.QUALITY_PROFILES["balanced"]["revision_rounds"] == 0
    assert rs.QUALITY_PROFILES["deep"]["revision_rounds"] == 2
    assert rs.QUALITY_PROFILES["balanced"]["model_space_expansion"] is False
    assert rs.QUALITY_PROFILES["balanced"]["deterministic_space_expansion"] is False
    assert rs.QUALITY_PROFILES["balanced"]["role_evidence_floor"] is False
    assert rs.QUALITY_PROFILES["balanced"]["author_effort"] == "high"
    assert rs.QUALITY_PROFILES["deep"]["author_effort"] == "max"
    assert rs.QUALITY_PROFILES["balanced"]["line_editor_effort"] == "high"
    assert rs.QUALITY_PROFILES["balanced"]["line_editor_timeout_seconds"] == 3 * 60
    assert rs.QUALITY_PROFILES["balanced"]["evaluator_effort"] == "high"
    assert rs.QUALITY_PROFILES["balanced"]["max_post_line_density_rounds"] == 2
    assert rs.MAX_SPACE_SWAP_CANDIDATES == 2
    assert rs.resume_evaluator.CODEX_EFFORT == "high"
    assert all(value == "high" for value in rs.CODEX_TASK_EFFORT_DEFAULTS.values())
    assert rs.QUALITY_PROFILES["balanced"]["audit_repair"] is False
    assert rs.QUALITY_PROFILES["balanced"]["evaluator_timeout_seconds"] == 8 * 60
    assert rs.QUALITY_PROFILES["deep"]["audit_repair"] is True
    assert rs.QUALITY_PROFILES["unchained"]["author_effort"] == "high"
    assert rs.QUALITY_PROFILES["unchained"]["audit_repair"] is False
    assert rs.QUALITY_PROFILES["unchained"]["target_opportunity_replacement"] is True
    assert rs.QUALITY_PROFILES["unchained"]["deterministic_space_expansion"] is False
    assert rs.QUALITY_PROFILES["search"]["deterministic_space_expansion"] is False
    assert rs.QUALITY_PROFILES["search_single"]["deterministic_space_expansion"] is False
    assert rs.QUALITY_PROFILES["search"]["role_evidence_floor"] is False
    assert rs.QUALITY_PROFILES["search_single"]["role_evidence_floor"] is False
    assert len(rs.canonical_control_prompt(_fixture_catalog())) <= rs.CANONICAL_CONTROL_PROMPT_CHARS


def test_unchained_generation_uses_quality_first_profile(monkeypatch, tmp_path):
    captured = {}

    def fake_run_tailoring(run_dir, job, update, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(rs, "run_tailoring", fake_run_tailoring)
    rs.run_generation(tmp_path, {"company": "Acme"}, lambda *args, **kwargs: None)
    assert captured["generation"] is True
    assert captured["unrestricted"] is True
    assert captured["quality_profile"] == "unchained"


def test_canonical_control_plan_is_verbatim_source_fallback():
    catalog = _fixture_catalog()
    catalog["entries"]["experience:item0"]["bullets"][0]["source"] = "immutable/VictorJimenezResume.tex"
    plan = rs.canonical_control_plan(catalog)
    assert plan["experiences"][0]["bullets"][0]["text"] == catalog["entries"]["experience:item0"]["bullets"][0]["text"]
    assert plan["revision_notes"]


def test_role_control_falls_back_to_immutable_when_reference_is_not_approved(tmp_path, monkeypatch):
    cv = tmp_path / "CV"
    immutable = cv / "immutable"
    immutable.mkdir(parents=True)
    (immutable / "VictorJimenezResume.tex").write_text("canonical")
    (immutable / "VictorJimenezResume.pdf").write_bytes(b"canonical")
    monkeypatch.setenv("RADAR_ROOT", str(tmp_path))
    monkeypatch.setenv("CV_ROOT", str(cv))

    control = rs.resolve_comparison_control(tmp_path, {
        "id": "control-old", "source": "run", "entry_id": "abc123def456",
        "role_family": "general_swe_cloud", "label": "Old control",
    })

    assert control["id"] == rs.IMMUTABLE_COMPARISON_CONTROL_ID
    assert control["resolution"] == "fallback"
    assert "not owner-approved" in control["fallback_reason"]


def test_approved_role_control_resolves_only_a_tailored_winner(tmp_path, monkeypatch):
    cv = tmp_path / "CV"
    immutable = cv / "immutable"
    immutable.mkdir(parents=True)
    (immutable / "VictorJimenezResume.tex").write_text("canonical")
    (immutable / "VictorJimenezResume.pdf").write_bytes(b"canonical")
    run_dir = cv / ".resume_studio" / "runs" / "abc123def456"
    run_dir.mkdir(parents=True)
    (run_dir / "status.json").write_text(json.dumps({
        "approval_state": "approved", "pdf_filename": "approved_resume_ai.pdf",
    }))
    (run_dir / "report.json").write_text(json.dumps({
        "approval_state": "approved", "winner_version": "tailored",
    }))
    (run_dir / "resume.tex").write_text("approved role control")
    (run_dir / "approved_resume_ai.pdf").write_bytes(b"approved")
    monkeypatch.setenv("RADAR_ROOT", str(tmp_path))
    monkeypatch.setenv("CV_ROOT", str(cv))

    control = rs.resolve_comparison_control(tmp_path, {
        "id": "control-good", "source": "run", "entry_id": "abc123def456",
        "role_family": "general_swe_cloud", "label": "General SWE / Cloud",
    })

    assert control["available"] is True
    assert control["reference_only"] is True
    assert control["_baseline_tex"] == "approved role control"


def test_role_control_diff_shows_supported_term_losses_and_gains():
    result = rs.comparison_control_diff(
        {
            "id": "control-good", "label": "General SWE / Cloud",
            "role_family": "general_swe_cloud", "source": "run",
            "entry_id": "abc123def456", "available": True,
            "approved": True, "reference_only": True,
            "_baseline_tex": "Python SQL REST APIs",
        },
        {"terms": [
            {"term": "Python", "supported": True},
            {"term": "SQL", "supported": True},
            {"term": "Git", "supported": True},
        ]},
        "Python Git",
    )

    assert result["lost_terms"] == ["SQL"]
    assert result["gained_terms"] == ["Git"]
    assert result["scope"] == "secondary_reference"


def test_comparison_control_summary_does_not_expose_local_artifact_paths():
    summary = rs.comparison_control_summary({
        "id": "control-good", "source": "run", "entry_id": "abc123def456",
        "artifact": "CV/.resume_studio/runs/abc123def456/approved_resume_ai.pdf",
        "available": True, "approved": True, "reference_only": True,
    })

    assert summary["artifact"] == "approved tailored PDF"
    assert "CV/" not in json.dumps(summary)


def test_control_recovery_restores_omitted_canonical_proof_before_jury(tmp_path):
    entry_id = "experience:research"
    catalog = {"entries": {
        entry_id: {
            "id": entry_id,
            "kind": "experience",
            "company": "NJIT",
            "role": "Researcher",
            "dates": "2025 -- 2026",
            "location": "Newark, NJ",
            "bullets": [
                {"id": entry_id + ":b1", "source": "immutable/VictorJimenezResume.tex", "text": "Built a research pipeline"},
                {"id": entry_id + ":b2", "source": "immutable/VictorJimenezResume.tex", "text": "Ran 20,000 HPC epochs via SLURM and Bash"},
                {"id": entry_id + ":b3", "source": "CV/cv_full.tex", "text": "Improved a generic workflow across systems"},
            ],
        },
    }}
    plan = {
        "experiences": [{
            "source_id": entry_id,
            "bullets": [
                {"source_id": entry_id + ":b1"},
                {"source_id": entry_id + ":b3"},
            ],
        }],
        "projects": [],
        "leadership": [],
    }

    recovered, record = rs.deterministic_control_recovery(
        plan, catalog, {}, tmp_path,
    )

    assert record["status"] == "applied"
    assert [item["source_id"] for item in recovered["experiences"][0]["bullets"]] == [
        entry_id + ":b1", entry_id + ":b2",
    ]
    assert (tmp_path / "control_recovery.json").exists()


def test_control_recovery_respects_an_explicit_source_grounded_tradeoff(tmp_path):
    entry_id = "project:target"
    catalog = {"entries": {
        entry_id: {
            "id": entry_id,
            "kind": "project",
            "heading": "\\textbf{Target Project}",
            "bullets": [
                {"id": entry_id + ":b1", "source": "immutable/VictorJimenezResume.tex", "text": "Won a quantified canonical award"},
                {"id": entry_id + ":b2", "source": "immutable/VictorJimenezResume.tex", "text": "Built a canonical API"},
                {"id": entry_id + ":b3", "source": "CV/cv_full.tex", "text": "Built a target-specific REST interface"},
            ],
        },
    }}
    plan = {
        "experiences": [],
        "projects": [{
            "source_id": entry_id,
            "bullets": [
                {"source_id": entry_id + ":b2", "priority": 70},
                {"source_id": entry_id + ":b3", "priority": 90},
            ],
        }],
        "leadership": [],
        "decision_ledger": [{
            "action": "Replace canonical award",
            "current_evidence": entry_id + ":b1 — quantified canonical award",
            "replacement_or_exclusion": entry_id + ":b3 — target-specific REST interface",
            "target_signal": "REST",
            "why_stronger": "Directly answers the target's supported interface requirement.",
            "signal_lost": "Award proof",
        }],
    }

    recovered, record = rs.deterministic_control_recovery(plan, catalog, {}, tmp_path)

    assert entry_id + ":b3" in {
        item["source_id"] for item in recovered["projects"][0]["bullets"]
    }
    assert record["status"] == "applied" or record["status"] == "not_needed"
    assert record["skipped_explained"][0]["source_ids"] == [entry_id + ":b1"]


def test_control_recovery_displaces_redundant_addition_for_distinct_canonical_signal(tmp_path):
    entry_id = "project:workspace"
    catalog = {"entries": {
        entry_id: {
            "id": entry_id,
            "kind": "project",
            "heading": "\\textbf{Workspace}",
            "bullets": [
                {"id": entry_id + ":b1", "source": "immutable/VictorJimenezResume.tex", "text": "Selected for HackMIT from a 13% acceptance pool"},
                {"id": entry_id + ":b2", "source": "immutable/VictorJimenezResume.tex", "text": "Architected coordination across 4+ specialized AI agents"},
                {"id": entry_id + ":b3", "source": "immutable/VictorJimenezResume.tex", "text": "Built secure multi-user document vaults with role-based sharing"},
                {"id": entry_id + ":b6", "source": "CV/cv_full.tex", "text": "Enforced JWT-verified row-level document access"},
            ],
        },
    }}
    plan = {
        "experiences": [],
        "projects": [{"source_id": entry_id, "bullets": [
            {"source_id": entry_id + ":b1", "priority": 90},
            {"source_id": entry_id + ":b3", "priority": 90},
            {"source_id": entry_id + ":b6", "priority": 90},
        ]}],
        "leadership": [],
    }

    recovered, record = rs.deterministic_control_recovery(plan, catalog, {}, tmp_path)
    selected = [item["source_id"] for item in recovered["projects"][0]["bullets"]]

    assert entry_id + ":b2" in selected
    assert entry_id + ":b6" not in selected
    assert any(item["replaced_source_id"] == entry_id + ":b6" for item in record["actions"])


def test_generic_project_reorder_does_not_explain_lost_canonical_bullet():
    removed = {
        "source_id": "project:workspace:b2",
        "entry_id": "project:workspace",
        "text": "Architected coordination across 4+ specialized AI agents",
    }
    ledger = [{
        "action": "Reorder projects",
        "current_evidence": "The workspace project moved earlier for software relevance.",
        "replacement_or_exclusion": "Keep the strongest target-specific lines first.",
        "target_signal": "software engineering",
        "why_stronger": "The order improves the skim.",
        "signal_lost": "Some project detail moves lower.",
    }]

    assert rs._ledger_explains_removed_evidence(removed, ledger, {}) is None
    ledger[0]["current_evidence"] = "Removed project:workspace:b2 after comparing the orchestration line."
    assert rs._ledger_explains_removed_evidence(removed, ledger, {}) is ledger[0]


def test_validate_plan_reverts_uncited_technical_claim_merge():
    entry_id = "experience:source"
    b1 = entry_id + ":b1"
    b2 = entry_id + ":b2"
    catalog = {"entries": {
        entry_id: {
            "id": entry_id,
            "kind": "experience",
            "company": "Example",
            "role": "Researcher",
            "bullets": [
                {"id": b1, "source": "immutable/VictorJimenezResume.tex", "text": "Built a research dashboard"},
                {"id": b2, "source": "CV/cv_full.tex", "text": "Implemented C++ streaming modules"},
            ],
        },
    }}
    graph = {"nodes": [
        {"id": b1, "claim_allowed": True, "heading": "Example", "text": "Built a research dashboard"},
        {"id": b2, "claim_allowed": True, "heading": "Example", "text": "Implemented C++ streaming modules"},
    ]}
    plan = {
        "positioning_thesis": "Research software",
        "selected_evidence": [],
        "excluded_evidence": [],
        "experiences": [{"source_id": entry_id, "why": "", "bullets": [{
            "source_id": b1,
            "source_ids": [b1],
            "text": "Built C++ modules for a research dashboard",
            "evidence_ids": [b1],
            "priority": 90,
            "candidate_rationale": "Adds systems detail",
        }]}],
        "projects": [],
        "leadership": [],
        "revision_notes": [],
        "decision_ledger": [],
        "front_matter_policy": {"coursework": "keep", "awards": "keep"},
    }

    normalized, errors = rs.validate_plan(plan, catalog, enhance=True, graph=graph)

    assert not errors
    assert normalized["experiences"][0]["bullets"][0]["text"] == "Built a research dashboard"
    assert any("uncited claim anchor" in warning for warning in normalized["validation_warnings"])

    plan["experiences"][0]["bullets"][0]["source_ids"] = [b1, b2]
    plan["experiences"][0]["bullets"][0]["evidence_ids"] = [b1, b2]
    normalized, errors = rs.validate_plan(plan, catalog, enhance=True, graph=graph)
    assert not errors
    assert "C++" in normalized["experiences"][0]["bullets"][0]["text"]

    public_id = "github-readme:public"
    graph["nodes"].append({
        "id": public_id,
        "claim_allowed": False,
        "heading": "Public repository",
        "text": "Implemented C++ streaming modules",
    })
    plan["experiences"][0]["bullets"][0]["source_ids"] = [b1, public_id]
    plan["experiences"][0]["bullets"][0]["evidence_ids"] = [b1, public_id]
    normalized, errors = rs.validate_plan(plan, catalog, enhance=True, graph=graph)
    assert not errors
    assert normalized["experiences"][0]["bullets"][0]["text"] == "Built a research dashboard"


def test_role_evidence_floor_recovers_omitted_primary_track_project(tmp_path, monkeypatch):
    entries = {}
    for index in range(4):
        entry_id = "project:adjacent-%s" % index
        entries[entry_id] = {
            "id": entry_id,
            "kind": "project",
            "heading": "\\textbf{Adjacent Project %s}" % index,
            "bullets": [
                {"id": entry_id + ":b1", "source": "immutable/VictorJimenezResume.tex", "text": "Built a quantum simulation pipeline"},
                {"id": entry_id + ":b2", "source": "immutable/VictorJimenezResume.tex", "text": "Validated model output with quantified results"},
            ],
        }
    target_id = "project:multi-agent-workspace"
    entries[target_id] = {
        "id": target_id,
        "kind": "project",
        "heading": "\\textbf{Multi-Agent Workspace}",
        "bullets": [
            {"id": target_id + ":b1", "source": "immutable/VictorJimenezResume.tex", "text": "Built a React web application with real-time collaboration"},
            {"id": target_id + ":b2", "source": "immutable/VictorJimenezResume.tex", "text": "Implemented secure multi-user document access control"},
        ],
    }
    catalog = {"entries": entries}
    plan = {
        "experiences": [],
        "projects": [{
            "source_id": "project:adjacent-%s" % index,
            "bullets": [
                {"source_id": "project:adjacent-%s:b1" % index, "text": "Built a quantum simulation pipeline", "evidence_ids": ["project:adjacent-%s:b1" % index]},
                {"source_id": "project:adjacent-%s:b2" % index, "text": "Validated model output with quantified results", "evidence_ids": ["project:adjacent-%s:b2" % index]},
            ],
        } for index in range(4)],
        "leadership": [],
    }
    graph = {"nodes": [
        {"id": bullet["id"], "claim_allowed": True}
        for entry in entries.values() for bullet in entry["bullets"]
    ]}
    context = {
        "target_keywords": {"terms": [{
            "term": "react", "supported": True, "required": True,
            "source_ids": [target_id + ":b1"],
        }]},
        "job_intelligence": {
            "primary_role_track": "product_software",
            "secondary_role_tracks": ["systems_performance"],
            "track_confidence": "ambiguous",
            "requirements": [{
                "importance": "required", "role_relevance": "primary",
                "exact_terms": ["react"], "evidence_ids": [target_id + ":b1"],
            }],
        },
    }
    monkeypatch.setattr(rs, "pack_plan_to_page", lambda value, _catalog, _run_dir: (value, {"fake_pack": True}))

    recovered, receipt = rs.deterministic_role_evidence_floor(
        plan, catalog, context, graph, tmp_path,
    )

    assert receipt["status"] == "applied"
    assert target_id in {entry["source_id"] for entry in recovered["projects"]}
    assert len(recovered["projects"]) == 4
    assert len(receipt["actions"]) == 1
    assert receipt["actions"][0]["restored_project_id"] == target_id


def test_target_opportunity_replacement_surfaces_only_allowed_unused_source_line(
    tmp_path, monkeypatch,
):
    catalog = _fixture_catalog()
    plan = _fixture_plan()
    plan["experiences"][0]["bullets"] = [
        {"source_id": "experience:item0:b1"},
        {"source_id": "experience:item0:b2"},
    ]
    for section in ("experiences", "projects", "leadership"):
        for selection in plan[section]:
            entry = catalog["entries"][selection["source_id"]]
            text_by_id = {item["id"]: item["text"] for item in entry["bullets"]}
            for bullet in selection["bullets"]:
                bullet["text"] = text_by_id[bullet["source_id"]]
    graph = {
        "nodes": [
            {"id": bullet["id"], "claim_allowed": True}
            for entry in catalog["entries"].values()
            for bullet in entry["bullets"]
        ]
    }
    context = {
        "generation_strategy": {
            "requirements": [{
                "requirement": "Stakeholder presentation",
                "importance": "required",
                "evidence_status": "direct",
                "evidence_ids": ["experience:item0:b3"],
                "recommended_action": "surface",
                "candidate_angle": "Make the presentation evidence visible.",
            }],
        },
        "job_intelligence": {"role_tracks": ["backend_infrastructure"]},
    }

    monkeypatch.setattr(
        rs,
        "pack_plan_to_page",
        lambda value, _catalog, _run_dir: (value, {"fake_pack": True}),
    )
    monkeypatch.setattr(rs, "render_plan", lambda *_args, **_kwargs: "resume")
    monkeypatch.setattr(rs, "compile_resume", lambda _run_dir: {"compiled": True})
    monkeypatch.setattr(
        rs,
        "pdf_layout",
        lambda *_args, **_kwargs: {
            "compiled": True,
            "pages": 1,
            "overfull": False,
            "horizontal": {"pass": True, "near_wrap_count": 0},
        },
    )

    result, receipt = rs.deterministic_target_opportunity_replacement(
        plan, catalog, graph, context, tmp_path,
    )

    assert receipt["status"] == "applied"
    assert receipt["source_id"] == "experience:item0:b3"
    assert receipt["actual_added_source_ids"] == ["experience:item0:b3"]
    assert receipt["actual_removed_source_ids"] == []
    selected = rs._selected_bullet_ids(result)
    assert "experience:item0:b3" in selected
    assert "experience:item0:b1" in selected


def test_canonical_control_bonus_protects_high_information_base_line():
    catalog = _fixture_catalog()
    catalog["entries"]["experience:item0"]["sources"] = ["immutable/VictorJimenezResume.tex"]
    catalog["entries"]["experience:item0"]["bullets"][0]["source"] = "immutable/VictorJimenezResume.tex"
    catalog["entries"]["experience:item0"]["bullets"][0]["text"] = (
        "Reached 0.984 ROC-AUC on 24,000 player-seasons with stratified validation"
    )
    canonical = catalog["entries"]["experience:item0"]["bullets"][0]
    fresh = {"source_id": "project:new:b1", "priority": 95, "text": "Built a new API"}
    canonical_value = rs._control_bullet_value({"source_id": canonical["id"], "priority": 80, "text": canonical["text"]}, catalog)
    fresh_value = rs._control_bullet_value(fresh, catalog)
    assert canonical_value > fresh_value


def test_packer_removal_actions_never_delete_the_last_evidence_bullet():
    plan = {
        "experiences": [{
            "source_id": "experience:item0",
            "bullets": [
                {"source_id": "experience:item0:b1", "priority": 1, "text": "First evidence"},
                {"source_id": "experience:item0:b2", "priority": 2, "text": "Second evidence"},
            ],
        }],
        "projects": [],
        "leadership": [],
    }
    actions = rs._removal_actions(plan, _fixture_catalog())
    assert actions
    assert all(action[3] is not None for action in actions)


def test_adaptive_portfolio_uses_safety_ceiling_instead_of_density_floor():
    assert rs.MAX_DENSITY_GAP_PT == 24.0
    assert rs.MIN_TOTAL_BULLETS == 1
    assert rs.MAX_TOTAL_BULLETS == rs.MAX_RENDERED_BULLETS


def test_curator_preserves_agent_experience_order():
    catalog = _fixture_catalog()
    plan, errors = rs.validate_plan(_fixture_plan(), catalog, enhance=False)
    assert not errors
    plan["experiences"].reverse()
    curated = rs.curate_candidate_portfolio(plan, catalog)
    assert [entry["source_id"] for entry in curated["experiences"]] == [
        "experience:item2",
        "experience:item1",
        "experience:item0",
    ]


def test_methodology_curator_caps_density_sorts_strength_and_removes_duplicates():
    catalog = _fixture_catalog()
    plan, errors = rs.validate_plan(_fixture_plan(), catalog, enhance=False)
    assert not errors
    plan = rs.expand_candidate_portfolio(plan, catalog, enhance=True)
    for entry in plan["experiences"]:
        for index, bullet in enumerate(entry["bullets"]):
            bullet["priority"] = 90 - index
    duplicate = json.loads(json.dumps(plan["experiences"][0]["bullets"][0]))
    duplicate["source_id"] = "experience:item0:duplicate"
    duplicate["text"] = duplicate["text"].replace("Built item", "Built an item")
    duplicate["priority"] = 1
    plan["experiences"][0]["bullets"].append(duplicate)
    plan["projects"].append(
        {
            "source_id": "project:item-extra",
            "bullets": [{"source_id": "project:item-extra:b1", "text": "Built extra project", "priority": 1}],
            "why": "weak extra",
        }
    )
    curated = rs.curate_candidate_portfolio(plan)
    metrics = rs.portfolio_metrics(curated)
    assert metrics["pass"] is True
    assert metrics["total_bullets"] <= rs.MAX_TOTAL_BULLETS
    assert len(curated["experiences"]) <= 3
    assert len(curated["projects"]) <= rs.PORTFOLIO_CAPS["projects"]["entries"]
    assert len(curated["leadership"]) <= rs.PORTFOLIO_CAPS["leadership"]["entries"]
    assert all(len(entry["bullets"]) <= rs.PORTFOLIO_CAPS["experiences"]["bullets"] for entry in curated["experiences"])
    assert all(len(entry["bullets"]) <= rs.PORTFOLIO_CAPS["projects"]["bullets"] for entry in curated["projects"])


def test_curator_removes_same_entry_repeated_metric_story():
    plan = {
        "experiences": [],
        "projects": [{
            "source_id": "project:metrics",
            "bullets": [
                {"source_id": "project:metrics:b1", "text": "Calibrated wearable sensors in under a minute across 10,000 samples", "priority": 80},
                {"source_id": "project:metrics:b2", "text": "Improved wearable calibration to sub-minute performance over 10,000 samples", "priority": 40},
            ],
        }],
        "leadership": [],
    }
    curated = rs.curate_candidate_portfolio(plan)
    assert [item["source_id"] for item in curated["projects"][0]["bullets"]] == ["project:metrics:b1"]
    assert any(action["kind"] == "near_duplicate" for action in curated["portfolio_budget"]["actions"])


def test_final_portfolio_guard_catches_reintroduced_semantic_duplicate_and_keeps_stronger_line():
    plan = {
        "experiences": [],
        "projects": [{
            "source_id": "project:metrics",
            "bullets": [
                {"source_id": "project:metrics:b1", "text": "Calibrated wearable sensors in under a minute across 10,000 samples", "priority": 40},
                {"source_id": "project:metrics:b2", "text": "Improved wearable calibration to sub-minute performance over 10,000 samples", "priority": 90},
            ],
        }],
        "leadership": [],
    }
    guarded, receipt = rs.deterministic_final_portfolio_guard(plan, {})

    assert receipt["changed"] is True
    assert [item["source_id"] for item in guarded["projects"][0]["bullets"]] == ["project:metrics:b2"]
    assert receipt["removed_source_ids"] == ["project:metrics:b1"]


def test_portfolio_metrics_reports_same_entry_semantic_duplicates():
    plan = {
        "experiences": [],
        "projects": [{
            "source_id": "project:metrics",
            "bullets": [
                {"source_id": "project:metrics:b1", "text": "Calibrated wearable sensors in under a minute across 10,000 samples"},
                {"source_id": "project:metrics:b2", "text": "Improved wearable calibration to sub-minute performance over 10,000 samples"},
            ],
        }],
        "leadership": [],
    }

    metrics = rs.portfolio_metrics(plan)

    assert metrics["pass"] is False
    assert metrics["duplicates"] == [["project:metrics:b1", "project:metrics:b2"]]


def test_packer_never_deletes_content_to_recover_from_compile_error(monkeypatch, tmp_path):
    plan = _fixture_plan()
    original_total = rs.portfolio_metrics(plan)["total_bullets"]
    catalog = _fixture_catalog()
    monkeypatch.setattr(
        rs,
        "_compile_plan_attempt",
        lambda *args, **kwargs: (
            "bad tex",
            {"compiled": False, "pages": None, "overfull": False},
        ),
    )
    try:
        rs.pack_plan_to_page(plan, catalog, tmp_path)
    except RuntimeError as exc:
        assert "syntax error as page overflow" in str(exc)
    else:
        raise AssertionError("compile failure should stop packing")
    assert rs.portfolio_metrics(plan)["total_bullets"] == original_total


def test_packer_preserves_evidence_when_only_horizontal_safety_is_pending(monkeypatch, tmp_path):
    plan = _fixture_plan()
    catalog = _fixture_catalog()
    original_total = rs.portfolio_metrics(plan)["total_bullets"]
    monkeypatch.setattr(
        rs,
        "_compile_plan_attempt",
        lambda *args, **kwargs: (
            "safe-page-but-tight tex",
            {
                "compiled": True,
                "pages": 1,
                "overfull": False,
                "horizontal": {
                    "bullets": [{"source_id": "experience:item0:b1", "near_wrap": True}],
                    "pass": False,
                    "wrap_count": 0,
                    "near_wrap_count": 1,
                },
                "density_gap_pt": 0.0,
                "density_pass": True,
            },
        ),
    )
    packed, receipt = rs.pack_plan_to_page(plan, catalog, tmp_path)
    assert rs.portfolio_metrics(packed)["total_bullets"] == original_total
    assert receipt["horizontal_pass"] is False


def test_short_one_line_bullet_is_not_failed_for_unused_right_margin(monkeypatch, tmp_path):
    monkeypatch.setattr(
        rs,
        "pdf_line_geometry",
        lambda pdf: {
            "page_width": 612.0,
            "lines": [
                {
                    "text": "Built concise system",
                    "x_min": 60.0,
                    "x_max": 250.0,
                    "y_min": 100.0,
                    "y_max": 109.0,
                }
            ],
        },
    )
    plan = {
        "experiences": [
            {"bullets": [{"source_id": "b1", "text": "Built concise system"}]}
        ],
        "projects": [],
        "leadership": [],
    }
    result = rs.bullet_layout_metrics(plan, tmp_path / "resume.pdf")
    assert result["underfilled_line_count"] == 1
    assert result["pass"] is True


def test_near_wrap_bullet_fails_with_safe_right_slack(monkeypatch, tmp_path):
    monkeypatch.setattr(
        rs,
        "pdf_line_geometry",
        lambda pdf: {
            "page_width": 612.0,
            "lines": [
                {
                    "text": "Built concise system",
                    "x_min": 60.0,
                    "x_max": 598.5,
                    "y_min": 100.0,
                    "y_max": 109.0,
                }
            ],
        },
    )
    result = rs.bullet_layout_metrics(
        {"experiences": [{"bullets": [{"source_id": "b1", "text": "Built concise system"}]}]},
        tmp_path / "resume.pdf",
    )
    assert result["near_wrap_count"] == 1
    assert result["pass"] is False
    assert result["bullets"][0]["horizontal_pass"] is False


def test_target_keyword_strategy_marks_supported_and_unsupported_terms(tmp_path):
    cv = tmp_path / "CV"
    cv.mkdir()
    (cv / "immutable").mkdir()
    (cv / "immutable" / "resume.tex").write_text("Python SQL PyTorch\\n")
    (cv / "cv_full.tex").write_text("Python SQL PyTorch\\n")
    context = {
        "company": "Example",
        "title": "ML Engineer",
        "posting_text": "Qualifications: required Python, SQL, PyTorch, and CUDA experience. "
        + "The engineer will build reliable systems, collaborate with teams, and document technical work. "
        + "This posting includes additional context so the captured description is long enough for full-posting analysis. "
        + "Candidates should explain design decisions, test changes, and communicate tradeoffs clearly to reviewers.",
    }
    strategy = rs.target_keyword_strategy(context, _fixture_catalog(), tmp_path)
    terms = {item["term"]: item for item in strategy["terms"]}
    assert terms["python"]["supported"] is True
    assert terms["pytorch"]["supported"] is True
    assert terms["cuda"]["supported"] is False
    assert "python" in strategy["required_terms"]


def test_generation_keyword_strategy_searches_authorized_markdown_and_ignores_denials(tmp_path):
    cv = tmp_path / "CV"
    (cv / "immutable").mkdir(parents=True)
    (cv / "immutable" / "resume.tex").write_text("Python GitHub\n")
    (cv / "cv_full.tex").write_text("Python GitHub\n")
    graph = {
        "nodes": [
            {"id": "doc:sharepoint", "heading": "Workflow", "text": "Used SharePoint and version control for team documentation", "claim_allowed": True},
            {"id": "doc:boundary", "heading": "Boundary", "text": "This does not authorize Agile experience", "claim_allowed": True},
        ]
    }
    context = {
        "posting_text": (
            "Required skills include version control and testing. Responsibilities include SharePoint and GitHub. "
            "Preferred experience includes Agile methodology and AWS. This description includes enough additional "
            "context for exact posting analysis and requirement extraction across software-development workflows."
        )
    }
    strategy = rs.target_keyword_strategy(
        context, _fixture_catalog(), tmp_path, graph=graph, comprehensive=True,
    )
    terms = {item["term"]: item for item in strategy["terms"]}
    assert terms["sharepoint"]["supported"] is True
    assert terms["version control"]["supported"] is True
    assert terms["agile"]["supported"] is False
    assert terms["aws"]["supported"] is False


def test_denial_detector_rejects_postfix_unsupported_language():
    assert rs._keyword_affirmed(
        "aws", "Docker is supported; Linux administration and AWS are unsupported."
    ) is False
    assert rs._keyword_affirmed(
        "docker", "Docker is supported; Linux administration and AWS are unsupported."
    ) is True
    assert rs._keyword_affirmed(
        "docker", "Docker is supported and AWS is unsupported."
    ) is True


def test_gap_analysis_keeps_unsupported_terms_visible_and_promotes_adjacent_support():
    catalog = _fixture_catalog()
    graph = {
        "nodes": [
            {"id": "doc:cloud", "heading": "Cloud architecture", "text": "Built a Google Cloud application", "claim_allowed": True},
        ]
    }
    keywords = {
        "terms": [
            {"term": "cloud computing", "importance": "preferred", "supported": False, "source_ids": []},
            {"term": "aws", "importance": "responsibility", "supported": False, "source_ids": []},
        ]
    }
    data = {
        "portfolio_strategy": "Expose defensible cloud evidence.",
        "requirements": [{
            "requirement": "cloud computing",
            "importance": "preferred",
            "exact_terms": ["cloud computing"],
            "evidence_status": "adjacent",
            "evidence_ids": ["doc:cloud"],
            "target_entry_id": "experience:item0",
            "recommended_action": "synthesize",
            "candidate_angle": "Use Google Cloud implementation as the proof.",
            "reason": "The capability is direct even though the source uses the platform name.",
        }, {
            "requirement": "Evidence review is in progress",
            "importance": "required",
            "exact_terms": ["output format"],
            "evidence_status": "direct",
            "evidence_ids": [],
            "target_entry_id": "",
            "recommended_action": "keep",
            "candidate_angle": "",
            "reason": "Process narration must not enter the job analysis.",
        }, {
            "requirement": "Scientific domains",
            "importance": "preferred",
            "exact_terms": ["cloud computing", "aws"],
            "evidence_status": "adjacent",
            "evidence_ids": ["doc:cloud"],
            "target_entry_id": "experience:item0",
            "recommended_action": "rewrite",
            "candidate_angle": "Use cloud evidence, but do not claim AWS.",
            "reason": "AWS is unsupported.",
        }],
        "must_cover_terms": ["cloud computing", "aws"],
        "honest_gaps": ["aws"],
    }
    normalized = rs.normalize_gap_analysis(
        data, keywords, catalog, graph,
        "Preferred cloud computing experience; applications may use AWS.",
    )
    enriched = rs.apply_gap_support_to_keywords(keywords, normalized)
    terms = {item["term"]: item for item in enriched["terms"]}
    assert terms["cloud computing"]["supported"] is True
    assert terms["cloud computing"]["support_kind"] == "adjacent"
    assert terms["aws"]["supported"] is False
    assert normalized["must_cover_terms"] == ["cloud computing"]
    assert "cloud computing" not in normalized["honest_gaps"]
    assert all(item["requirement"] != "Evidence review is in progress" for item in normalized["requirements"])
    assert all("aws" not in item["exact_terms"] for item in normalized["requirements"] if item["evidence_status"] != "unsupported")
    assert "aws" in normalized["honest_gaps"]


def test_gap_analysis_drops_semantically_misplaced_inventory_terms():
    catalog = _fixture_catalog()
    graph = {
        "nodes": [{
            "id": "doc:react",
            "heading": "React workspace",
            "text": "Built a React client application",
            "claim_allowed": True,
        }]
    }
    keywords = {
        "terms": [{
            "term": "software engineering", "importance": "preferred",
            "supported": True, "source_ids": ["doc:react"],
        }]
    }
    normalized = rs.normalize_gap_analysis(
        {
            "requirements": [{
                "requirement": "Experience with client-side frameworks such as AngularJS",
                "importance": "preferred",
                "exact_terms": ["software engineering"],
                "evidence_status": "adjacent",
                "evidence_ids": ["doc:react"],
                "target_entry_id": "experience:item0",
                "recommended_action": "tailor_skills",
                "candidate_angle": "Surface React as the supported framework; do not claim AngularJS.",
                "reason": "React is adjacent, AngularJS is unsupported.",
            }],
            "must_cover_terms": [], "honest_gaps": [],
            "portfolio_strategy": "Keep framework evidence precise.",
        },
        keywords, catalog, graph,
        "Preferred software engineering experience with AngularJS frameworks.",
    )
    assert all(
        "client-side frameworks" not in item["requirement"]
        for item in normalized["requirements"]
    )


def test_content_change_report_exposes_rewrites_and_project_swaps():
    catalog = _fixture_catalog()
    catalog["entries"]["project:item0"]["sources"] = ["resume.tex"]
    catalog["entries"]["project:item1"]["sources"] = ["resume.tex"]
    catalog["entries"]["project:item2"]["sources"] = ["cv_full.tex"]
    plan, errors = rs.validate_plan(_fixture_plan(), catalog, enhance=False)
    assert not errors
    plan["projects"] = [plan["projects"][1]]
    plan["projects"].append({
        "source_id": "project:item2",
        "bullets": [{"source_id": "project:item2:b1", "text": catalog["entries"]["project:item2"]["bullets"][0]["text"], "priority": 90}],
        "why": "better target fit",
    })
    plan["experiences"][0]["bullets"][0]["text"] = "\\textbf{Rewritten evidence line}"
    changes = rs.content_change_report(plan, catalog, "Python SQL")
    assert changes["changed_bullet_count"] == 1
    assert changes["project_swaps"]["swapped_in"] == ["\\textbf{Project 2} | \\emph{Python}"]
    assert changes["project_swaps"]["swapped_out"] == ["\\textbf{Project 0} | \\emph{Python}"]
    assert "changed" in changes["experience_order"]


def test_content_change_report_uses_immutable_canonical_project_sources():
    catalog = _fixture_catalog()
    catalog["entries"]["project:item0"]["sources"] = ["immutable/VictorJimenezResume.tex"]
    catalog["entries"]["project:item1"]["sources"] = ["immutable/VictorJimenezResume.tex"]
    catalog["entries"]["project:item2"]["sources"] = ["cv_full.tex"]
    plan, errors = rs.validate_plan(_fixture_plan(), catalog, enhance=False)
    assert not errors
    plan["projects"] = [plan["projects"][1], {
        "source_id": "project:item2",
        "bullets": [{"source_id": "project:item2:b1", "priority": 90}],
        "why": "stronger target evidence",
    }]
    changes = rs.content_change_report(plan, catalog, "")
    assert changes["project_swaps"]["swapped_in"] == ["\\textbf{Project 2} | \\emph{Python}"]
    assert changes["project_swaps"]["swapped_out"] == ["\\textbf{Project 0} | \\emph{Python}"]


def test_job_intelligence_separates_fit_tracks_terms_and_hard_eligibility():
    intelligence = rs.build_job_intelligence(
        {"id": "job-1", "title": "Performance Engineer", "company": "Example"},
        "Required: CUDA, Python, and at least 1 years of experience. "
        "Work on distributed systems and performance optimization. " * 8,
        match={"score": 62, "confidence": "high", "missing_requirements": ["CUDA"]},
        target_keywords={"terms": [
            {"term": "Python", "importance": "required", "supported": True, "source_ids": ["cv:b1"], "support_kind": "exact"},
            {"term": "CUDA", "importance": "required", "supported": False, "source_ids": [], "support_kind": "none"},
        ]},
    )
    assert intelligence["version"] == rs.JOB_INTELLIGENCE_VERSION
    assert intelligence["fit"]["band"] == "moderate"
    assert "systems_performance" in intelligence["role_tracks"]
    assert "1+ years of experience required" in intelligence["hard_blockers"]
    cuda = next(item for item in intelligence["requirements"] if item["requirement"] == "CUDA")
    assert cuda["evidence_status"] == "unsupported"
    assert cuda["recommended_action"] == "leave_gap"
    assert intelligence["primary_role_track"] == "systems_performance"
    assert intelligence["role_focus"]["primary_label"] == "systems / performance / networking"


def test_role_focus_prioritizes_networking_over_generic_software_boilerplate():
    intelligence = rs.build_job_intelligence(
        {"title": "Software Development Engineer"},
        (
            "Build and optimize network performance for distributed systems. "
            "Work on low-latency services, throughput, Linux, and scalable "
            "infrastructure. Collaborate with software development teams."
        ),
    )
    assert intelligence["primary_role_track"] == "systems_performance"
    assert "backend_infrastructure" in intelligence["role_tracks"]
    assert intelligence["role_focus"]["confidence"] in {"moderate", "high", "ambiguous"}


def test_role_focus_marks_adjacent_requirements_without_flattening_them():
    intelligence = rs.build_job_intelligence(
        {"title": "Software Engineer"},
        "Build a web application with REST APIs and unit testing.",
        target_keywords={"terms": [
            {"term": "REST", "importance": "required", "supported": True, "source_ids": ["cv:b1"]},
            {"term": "unit testing", "importance": "required", "supported": False, "source_ids": []},
        ]},
    )
    rest = next(item for item in intelligence["requirements"] if item["requirement"] == "REST")
    testing = next(item for item in intelligence["requirements"] if item["requirement"] == "unit testing")
    assert rest["role_relevance"] in {"primary", "secondary", "general"}
    assert testing["evidence_status"] == "unsupported"
    assert testing["recommended_action"] == "leave_gap"


def test_content_change_report_marks_base_term_gains_and_losses():
    catalog = _fixture_catalog()
    plan, errors = rs.validate_plan(_fixture_plan(), catalog, enhance=False)
    assert not errors
    strategy = {"posting_available": True, "terms": [
        {"term": "Python", "importance": "required", "required": True, "supported": True, "source_ids": ["experience:item0:b1"]},
        {"term": "SQL", "importance": "preferred", "preferred": True, "supported": True, "source_ids": ["experience:item0:b1"]},
    ]}
    changes = rs.content_change_report(plan, catalog, "Python", strategy, base_tex="Python SQL")
    terms = {item["term"]: item for item in changes["keyword_coverage"]["terms"]}
    assert terms["Python"]["comparison_status"] == "retained"
    assert terms["SQL"]["comparison_status"] == "lost"
    assert changes["keyword_coverage"]["lost_terms"] == ["SQL"]
    assert changes["base_text_hash"]


def test_change_findings_catch_unsupported_terms_losses_and_missing_supported_evidence():
    findings = rs.build_change_findings(
        {"keyword_coverage": {"terms": [
                {"term": "CUDA", "supported": False, "rendered": True, "status": "unverified_rendered", "comparison_status": "gained"},
                {"term": "SQL", "supported": True, "rendered": False, "status": "missing", "comparison_status": "lost", "required": True, "source_ids": ["cv:sql"]},
                {"term": "Linux", "supported": True, "rendered": False, "status": "missing", "comparison_status": "absent", "source_ids": ["cv:linux"]},
        ], "portfolio_diagnostics": {"warnings": []}}},
        {"gates": {"factual": {"status": "fail", "reason": "unsupported term"}}},
    )
    classes = [item["classification"] for item in findings]
    assert "BLOCKER" in classes
    assert any(item["classification"] == "REGRESSION" and "SQL" in item["reason"] for item in findings)
    assert any(item["classification"] == "MISSED_OPPORTUNITY" and "Linux" in item["reason"] for item in findings)
    rewrite_findings = rs.build_change_findings({"rewritten_bullets": [{
        "source_id": "cv:bullet", "source_text": "Built Python APIs", "final_text": "Designed Python APIs",
    }]})
    assert rewrite_findings[0]["classification"] == "QUESTIONABLE"


def test_change_findings_count_only_panel_confirmed_added_evidence_as_gain():
    changes = {
        "added_bullets": [
            {"source_id": "experience:cie:b11", "text": "Developed a live analytics dashboard with alerts"},
            {"source_id": "project:weak:b1", "text": "Built a generic Python tool"},
        ],
        "keyword_coverage": {"terms": []},
        "portfolio_diagnostics": {},
    }
    review = {"portfolio_comparison": {
        "gained_strengths": ["Live posture analytics dashboard evidence."],
    }}
    findings = rs.build_change_findings(changes, {}, review)
    assert [item["source_ids"] for item in findings if item["classification"] == "KEEP_GOOD"] == [["experience:cie:b11"]]


def test_bare_layout_capacity_language_is_a_regression_not_a_hard_blocker():
    issue = (
        "The tailoring drops quantified evidence while the rendered layout still has capacity "
        "for another standard line."
    )
    assert rs.classify_critic_issue(issue) == "tailoring_regression"
    assert rs.classify_critic_issue("The rendered layout has a material failure with two wraps.") == "hard_blocker"


def test_change_findings_do_not_call_low_priority_context_loss_a_regression():
    findings = rs.build_change_findings({
        "keyword_coverage": {"terms": [{
            "term": "algorithms", "supported": True, "rendered": False,
            "base_rendered": True, "comparison_status": "lost",
            "importance": "mentioned", "source_ids": ["CV/immutable/VictorJimenezResume.tex"],
        }]},
        "portfolio_diagnostics": {"warnings": ["advisory overlap"], "blocking_warnings": []},
    })
    assert not any(item["classification"] == "REGRESSION" for item in findings)
    assert any(item["classification"] == "QUESTIONABLE" and "context term" in item["reason"] for item in findings)
    assert any(item["classification"] == "QUESTIONABLE" and "advisory overlap" in item["reason"] for item in findings)


def test_explained_tradeoff_is_not_counted_as_missed_evidence():
    findings = rs.build_change_findings({
        "removed_canonical_bullets": [{
            "source_id": "project:quantum:b1", "entry_id": "project:quantum",
            "text": "Won 1st place among 650 competitors", "tradeoff_status": "explained",
        }],
        "unexplained_removed_bullets": [],
        "portfolio_diagnostics": {"warnings": [], "blocking_warnings": []},
    })
    assert findings == []


def test_tailoring_recommendation_prefers_base_when_comparison_regresses():
    audit = {
        "recommended_version": "base", "comparison": {
            "gain_weight": 1, "loss_weight": 4, "missed_opportunity_weight": 2,
        }, "finding_counts": {"QUESTIONABLE": 0},
    }
    assert rs.tailoring_audit_preference_key(audit)[0] == 1
    assert rs.tailoring_audit_preference_key({
        "recommended_version": "tailored", "comparison": {
            "gain_weight": 4, "loss_weight": 0, "missed_opportunity_weight": 0,
        }, "finding_counts": {"QUESTIONABLE": 0},
    }) > rs.tailoring_audit_preference_key(audit)


def test_final_winner_never_exposes_a_rejected_tailored_candidate_as_primary():
    assert rs.final_winner_version({"recommended_version": "tailored"}) == "tailored"
    assert rs.final_winner_version({"recommended_version": "review"}) == "base"
    assert rs.final_winner_version({"recommended_version": "base"}) == "base"
    assert rs.final_winner_version({"recommended_version": "blocked"}) == "base"


def test_base_winner_archives_candidate_without_touching_immutable_source(monkeypatch, tmp_path):
    cv = tmp_path / "CV"
    immutable = cv / "immutable"
    immutable.mkdir(parents=True)
    (immutable / "VictorJimenezResume.pdf").write_bytes(b"canonical-pdf")
    (immutable / "VictorJimenezResume.tex").write_text("canonical-tex")
    monkeypatch.setenv("RADAR_ROOT", str(tmp_path))
    monkeypatch.setenv("CV_ROOT", str(cv))
    run_dir = cv / ".resume_studio" / "runs" / "abc123def456"
    run_dir.mkdir(parents=True)
    (run_dir / "status.json").write_text(json.dumps({"pdf_filename": "company_resume_ai.pdf"}))
    (run_dir / "company_resume_ai.pdf").write_bytes(b"tailored-pdf")

    result = rs.adopt_base_control_winner(run_dir, {"recommended_version": "base"})

    assert result["winner_version"] == "base"
    assert (run_dir / "company_resume_ai.pdf").read_bytes() == b"canonical-pdf"
    assert (run_dir / "tailored_candidate.pdf").read_bytes() == b"tailored-pdf"
    assert (immutable / "VictorJimenezResume.pdf").read_bytes() == b"canonical-pdf"
    assert (run_dir / "base_control.tex").read_text() == "canonical-tex"


def test_repair_feedback_exposes_unexplained_control_losses_to_writer():
    feedback = rs.tailoring_repair_feedback(
        {"decision": "do_not_ship", "recommended_version": "base", "tailoring": "regressed", "findings": []},
        {
            "canonical_bullet_count": 12,
            "unexplained_removed_bullets": [{
                "source_id": "project:distinct:b2", "entry_id": "project:distinct",
                "text": "Built a distinct systems artifact",
            }],
            "keyword_coverage": {"terms": [{"term": "Git", "supported": True, "comparison_status": "lost"}]},
            "project_swaps": {"swapped_in": ["repetitive"], "swapped_out": ["distinctive"]},
            "portfolio_diagnostics": {"warnings": ["overlap"], "blocking_warnings": []},
            "explained_tradeoffs": [],
        },
    )
    assert feedback["comparison_control"]["unexplained_removed_bullets"][0]["source_id"] == "project:distinct:b2"
    assert feedback["comparison_control"]["lost_supported_terms"] == ["Git"]
    assert "canonical evidence as the control" in feedback["rules"][1]


def test_tailoring_audit_keeps_fit_separate_from_tailoring_and_blocks_hard_failures():
    context = {"posting_text": "Required: Python. " * 40, "job_intelligence": {
        "posting_available": True, "posting_snapshot_hash": "post", "hash": "job-intel",
        "hard_blockers": [],
    }}
    match = {"score": 82, "confidence": "high", "missing_requirements": []}
    graph = {"hash": "graph"}
    changes = {"keyword_coverage": {"terms": [{
        "term": "Python", "supported": True, "rendered": True,
        "comparison_status": "gained", "status": "covered", "required": True,
    }], "required_coverage_percent": 100}, "portfolio_diagnostics": {}}
    passing_gates = {name: {"status": "pass", "reason": "ok"} for name in rs.REVIEW_CRITERIA}
    passing_gates.update({"layout": {"status": "pass", "reason": "ok"}, "eligibility": {"status": "pass", "reason": "ok"}, "portfolio": {"status": "pass", "reason": "ok"}})
    review = {"gates": passing_gates, "independent_review": True, "ready": True, "unsupported_claims": []}
    audit = rs.build_tailoring_audit(
        {"id": "job-1", "title": "Engineer"}, context, match, graph, {}, changes,
        {"gates": passing_gates}, review, "Python", "Python CUDA",
    )
    assert audit["fit"]["band"] == "strong"
    assert audit["tailoring"] == "improved"
    assert audit["recommended_version"] == "tailored"
    assert audit["readiness"] == "ready"

    review_only = rs.build_tailoring_audit(
        {"id": "job-1", "title": "Engineer"}, context, match, graph, {}, changes,
        {"gates": passing_gates},
        {"gates": {name: {"status": "fail", "reason": "critic unavailable"} for name in rs.REVIEW_CRITERIA},
         "independent_review": False, "ready": False, "unsupported_claims": []},
        "Python", "Python",
    )
    assert review_only["readiness"] == "review"
    assert review_only["hard_failures"] == []

    critic_gates = dict(passing_gates)
    critic_gates["factual"] = {"status": "fail", "reason": "critic found a vague unsupported claim"}
    critic_blocked = rs.build_tailoring_audit(
        {"id": "job-1", "title": "Engineer"}, context, match, graph, {}, changes,
        {"gates": passing_gates},
        {"gates": critic_gates, "independent_review": True, "ready": False, "unsupported_claims": []},
        "Python", "Python",
    )
    assert critic_blocked["readiness"] == "blocked"
    assert critic_blocked["hard_failures"][0]["name"] == "factual"

    blocked = rs.build_tailoring_audit(
        {"id": "job-1", "title": "Engineer"}, context, match, graph, {}, changes,
        {"gates": {"eligibility": {"status": "fail", "reason": "degree conflict"}}},
        {"gates": {"eligibility": {"status": "fail", "reason": "degree conflict"}}, "independent_review": True, "ready": False, "unsupported_claims": []},
        "Python", "Python",
    )
    assert blocked["fit"]["band"] == "strong"
    assert blocked["readiness"] == "blocked"


def test_tailoring_audit_labels_a_noop_as_unchanged_not_regressed():
    context = {"posting_text": "Required: Python. " * 40, "job_intelligence": {
        "posting_available": True, "posting_snapshot_hash": "post", "hash": "job-intel",
        "hard_blockers": [],
    }}
    match = {"score": 82, "confidence": "high", "missing_requirements": []}
    graph = {"hash": "graph"}
    gates = {name: {"status": "pass", "reason": "ok"} for name in rs.REVIEW_CRITERIA}
    gates.update({"layout": {"status": "pass", "reason": "ok"}, "eligibility": {"status": "pass", "reason": "ok"}, "portfolio": {"status": "pass", "reason": "ok"}})
    review = {
        "gates": gates,
        "independent_review": True,
        "ready": True,
        "unsupported_claims": [],
        "blocking_issues": ["The selected portfolio repeats an existing backend story."],
    }
    audit = rs.build_tailoring_audit(
        {"id": "job-1", "title": "Engineer"}, context, match, graph, {},
        {"keyword_coverage": {"terms": [], "gained_terms": [], "lost_terms": []}, "portfolio_diagnostics": {}},
        {"gates": gates}, review, "Python", "Python",
    )
    assert audit["tailoring"] == "unchanged"
    assert audit["candidate_delta"]["material"] is False
    assert audit["recommended_version"] == "base"
    assert audit["decision"] == "prefer_base"
    assert audit["finding_counts"]["REGRESSION"] == 0
    assert any(item["classification"] == "QUESTIONABLE" for item in audit["findings"])



def test_tailoring_audit_summary_keeps_gains_and_not_questionable_improved():
    audit = {
        "version": rs.TAILORING_AUDIT_VERSION,
        "status": "review", "readiness": "review", "tailoring": "inconclusive", "confidence": "medium",
        "fit": {"band": "moderate"}, "finding_counts": {"QUESTIONABLE": 1},
        "findings": [
            {"classification": "KEEP_GOOD", "severity": "info", "reason": "Supported Python was surfaced."},
            {"classification": "QUESTIONABLE", "severity": "warning", "reason": "A duplicate was suppressed."},
        ],
        "hash": "audit-hash",
    }
    summary = rs.tailoring_audit_summary(audit)
    assert summary["gains"] == ["Supported Python was surfaced."]
    assert summary["losses"] == []
    assert summary["fit"] == "moderate"


def test_tailoring_audit_summary_carries_only_opaque_run_correlation():
    summary = rs.tailoring_audit_summary({
        "run_id": "run-1", "queue_id": "queue-1",
        "findings": [{"classification": "BLOCKER", "reason": "Independent critic reported unsupported claim: private candidate evidence"}],
    })
    assert summary["run_id"] == "run-1"
    assert summary["queue_id"] == "queue-1"
    assert "private candidate evidence" not in summary["blockers"][0]


def test_portfolio_diagnostics_flags_leadership_and_repeated_agents_story():
    catalog = _fixture_catalog()
    catalog["entries"]["experience:item0"]["bullets"][0]["text"] = "Built an agentic RAG FastAPI backend"
    catalog["entries"]["project:item0"]["heading"] = "Multi-Agent Workspace | RAG"
    catalog["entries"]["project:item0"]["bullets"][0]["text"] = "Built agents and retrieval APIs"
    catalog["entries"]["project:item1"]["heading"] = "Spatial System | Flask"
    catalog["entries"]["project:item1"]["bullets"][0]["text"] = "Built a Flask backend with secure access control"
    catalog["entries"]["leadership:item0"]["bullets"][0]["text"] = "Led students through community programs"
    plan = {
        "experiences": [{
            "source_id": "experience:item0",
            "bullets": [{"source_id": "experience:item0:b1", "text": "Built an agentic RAG FastAPI backend"}],
        }],
        "projects": [{
            "source_id": "project:item0",
            "bullets": [{"source_id": "project:item0:b1", "text": "Built agents and retrieval APIs"}],
        }],
        "leadership": [{
            "source_id": "leadership:item0",
            "bullets": [{"source_id": "leadership:item0:b1", "text": "Led students through community programs"}],
        }],
    }
    diagnostics = rs.portfolio_diagnostics(plan, catalog)
    assert diagnostics["project_overlap"][0]["severity"] == "high"
    assert diagnostics["leadership_competition"]
    assert diagnostics["warnings"]


def test_front_matter_policy_reclaims_only_sanctioned_optional_lines(tmp_path):
    cv = tmp_path / "CV"
    immutable = cv / "immutable"
    immutable.mkdir(parents=True)
    (immutable / "VictorJimenezResume.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\n"
        "Victor Jimenez | vmj@njit.edu\n"
        "%-----------EDUCATION-----------\n"
        "\\resumeSubheading{School}{Location}{Degree}{Dates}\n"
        "\\resumeItem{\\textbf{GPA:} 3.5}\n"
        "\\resumeItem{\\textbf{Coursework:} Data Structures, Linear Algebra}\n"
        "%-----------SKILLS-----------\n"
        "\\resumeItem{\\textbf{Languages:} Python}\n"
        "\\resumeItem{\\textbf{Awards:} HackRU}\n"
        + rs.BODY_MARKER
        + "\nold body\n\\end{document}\n"
    )
    catalog = _fixture_catalog()
    plan = {
        "experiences": [], "projects": [], "leadership": [],
        "front_matter_policy": {"coursework": "omit", "awards": "omit"},
    }
    tex = rs.render_plan(plan, catalog, tmp_path)
    assert "Coursework:" not in tex
    assert "Awards:" not in tex
    assert "GPA:" in tex
    guard = rs.template_style_guard(tex, tmp_path, plan["front_matter_policy"])
    assert guard["passed"] is True


def test_flexible_content_reclaim_order_protects_technical_evidence():
    plan = {
        "experiences": [],
        "projects": [{
            "source_id": "project:hackmit",
            "bullets": [
                {"source_id": "project:hackmit:b1", "text": "Built secure workspace"},
                {"source_id": "project:hackmit:b2", "text": "Selected for HackMIT from a 13% acceptance pool"},
            ],
        }],
        "leadership": [],
        "front_matter_policy": {"coursework": "keep", "awards": "keep"},
    }

    first = rs._reclaim_flexible_content(plan)
    assert first["field"] == "coursework"
    assert plan["front_matter_policy"]["coursework"] == "omit"

    second = rs._reclaim_flexible_content(plan)
    assert second["field"] == "awards"
    assert plan["front_matter_policy"]["awards"] == "omit"
    assert len(plan["projects"][0]["bullets"]) == 2

    third = rs._reclaim_flexible_content(plan)
    assert third is None


def test_validation_preserves_and_normalizes_decision_ledger():
    catalog = _fixture_catalog()
    plan = _fixture_plan()
    plan["decision_ledger"] = [{
        "action": "swap",
        "current_evidence": "Project 0: generic project evidence",
        "replacement_or_exclusion": "Project 2: stronger systems evidence",
        "target_signal": "backend engineering",
        "why_stronger": "adds a materially stronger implementation thread",
        "signal_lost": "one project-specific signal",
        "unexpected": "discarded",
    }]
    normalized, errors = rs.validate_plan(plan, catalog, enhance=False)
    assert not errors
    assert normalized["decision_ledger"][0]["action"] == "swap"
    assert "unexpected" not in normalized["decision_ledger"][0]


def test_validation_rebuckets_known_entries_returned_under_wrong_section():
    catalog = _fixture_catalog()
    plan = _fixture_plan()
    misplaced = plan["experiences"].pop()
    plan["projects"].append(misplaced)
    normalized, errors = rs.validate_plan(plan, catalog, enhance=False)
    assert not errors
    assert any(item["source_id"] == "experience:item2" for item in normalized["experiences"])
    assert all(item["source_id"] != "experience:item2" for item in normalized["projects"])
    assert any("reclassified experience:item2" in warning for warning in normalized["validation_warnings"])


def test_prompts_teach_marginal_hiring_value_without_a_preserve_base_rule():
    catalog = _fixture_catalog()
    base = rs.base_prompt({"company": "Example"}, "editor", catalog, True)
    synthesis = rs.synthesis_prompt({"company": "Example"}, [{"provider": "codex", "data": _fixture_plan()}], catalog, True)
    review = rs.reviewer_prompt({"company": "Example"}, "resume", plan=_fixture_plan(), catalog=catalog)
    revision = rs.revision_prompt({"company": "Example"}, _fixture_plan(), {"decision_feedback": []}, catalog)
    for prompt in (base, synthesis, review, revision):
        assert "hiring-value gain" in prompt or "hiring value" in prompt
        assert "decision_ledger" in prompt or "decision_feedback" in prompt
    assert "do not apply a blanket" in review
    assert "Resident Assistant" in base
    assert "project slot" in synthesis


def test_generation_prompt_includes_the_binding_gap_strategy_outside_truncated_context():
    context = {
        "company": "Example",
        "posting_text": "x" * (rs.MAX_CONTEXT_PROMPT_CHARS + 100),
        "generation_strategy": {
            "portfolio_strategy": "Surface the verified SharePoint workflow",
            "requirements": [],
        },
    }
    prompt = rs.base_prompt(
        context, "editor", _fixture_catalog(), True,
        unrestricted=True, generation=True,
    )
    assert "Binding requirement-to-evidence strategy" in prompt
    assert "Surface the verified SharePoint workflow" in prompt


def test_owner_notes_current_regression_benchmark_reaches_provider_context():
    context = rs.resume_authority_context(rs.repo_root())
    if not context:
        pytest.skip("private CV authority corpus is intentionally absent from CI")
    assert "Google SWE regression benchmark" in context
    assert "keep Quantum Stock Simulator omitted" in context


def test_renderer_keeps_canonical_prefix_and_company_first(tmp_path):
    cv = tmp_path / "CV"
    cv.mkdir()
    (cv / "immutable").mkdir()
    (cv / "immutable" / "VictorJimenezResume.tex").write_text(
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "Victor Jimenez | vmj@njit.edu\n"
        "\\section{Education}\nEducation content\n"
        "\\section{Technical Skills}\nSkills content\n"
        + rs.BODY_MARKER
        + "\nold body\n\\end{document}\n"
    )
    catalog = _fixture_catalog()
    plan, errors = rs.validate_plan(_fixture_plan(), catalog, enhance=False)
    assert not errors
    tex = rs.render_plan(plan, catalog, tmp_path)
    assert tex.startswith("\\documentclass{article}\n\\begin{document}\nVictor Jimenez")
    assert "{\\large Company 0}{2025 -- 2026}\n    {Role 0}{Newark, NJ}" in tex
    guard = rs.template_style_guard(tex, tmp_path)
    assert guard["passed"] is True
    assert guard["font_size_reduction_percent"] == 0.0


def test_renderer_omits_sections_the_adaptive_plan_does_not_need(tmp_path):
    cv = tmp_path / "CV"
    cv.mkdir()
    (cv / "immutable").mkdir()
    (cv / "immutable" / "VictorJimenezResume.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\n"
        "Victor Jimenez | vmj@njit.edu\n"
        + rs.BODY_MARKER
        + "\nold body\n\\end{document}\n"
    )
    catalog = _fixture_catalog()
    plan, errors = rs.validate_plan(_fixture_plan(), catalog, enhance=False)
    assert not errors
    plan["experiences"] = []
    plan["leadership"] = []
    tex = rs.render_plan(plan, catalog, tmp_path)
    body = tex.split(rs.BODY_MARKER, 1)[1]
    assert "\\section{Experience}" not in body
    assert "Leadership \\& Extracurriculars" not in body
    assert "\\section{Projects}" in body


def test_provider_transcript_uses_final_structured_response(tmp_path):
    stdout = tmp_path / "stdout.txt"
    stderr = tmp_path / "stderr.txt"
    final = {
        "positioning_thesis": "targeted engineer",
        "selected_evidence": [], "excluded_evidence": [],
        "experiences": [{"source_id": "experience:a"}],
        "projects": [{"source_id": "project:a"}],
        "leadership": [], "revision_notes": [],
    }
    stdout.write_text("progress\n" + json.dumps(final) + "\n")
    stderr.write_text(
        "prompt context containing a decoy plan\n"
        + json.dumps({
            "positioning_thesis": "prompt echo",
            "experiences": [], "projects": [], "leadership": [],
        })
        + "\n"
    )
    data = rs.provider_data_from_files(stdout, stderr, "draft")
    assert data["experiences"][0]["source_id"] == "experience:a"


def test_provider_transcript_ignores_stderr_prompt_echo_when_output_is_empty(tmp_path):
    stdout = tmp_path / "stdout.txt"
    stderr = tmp_path / "stderr.txt"
    stdout.write_text("")
    stderr.write_text(json.dumps({
        "positioning_thesis": "prompt echo",
        "experiences": [], "projects": [], "leadership": [],
    }))
    assert rs.provider_data_from_files(stdout, stderr, "draft") is None


def test_validate_plan_repairs_a_typo_to_a_claim_authorized_source_bullet():
    catalog = {
        "entries": {
            "project:rest": {
                "id": "project:rest", "kind": "project",
                "heading": "\\textbf{REST Project}",
                "bullets": [{
                    "id": "project:rest:b1",
                    "text": "\\textbf{Built 11 REST endpoints} for a client workflow",
                }],
            },
        },
    }
    graph = {"nodes": [{
        "id": "project:rest:b1", "claim_allowed": True,
        "text": "Built 11 REST endpoints for a client workflow",
    }]}
    plan, errors = rs.validate_plan({
        "experiences": [],
        "projects": [{
            "source_id": "project:rest",
            "bullets": [{
                "source_id": "project:rest:b1",
                "text": "\\textbf{Built 11 REST endpoints} for a client workflow",
                "evidence_ids": ["project:rest:rest:b1"],
            }],
        }],
        "leadership": [],
    }, catalog, enhance=True, graph=graph)
    assert not errors
    assert plan["projects"][0]["bullets"][0]["evidence_ids"] == ["project:rest:b1"]
    assert any("repaired evidence citation" in warning for warning in plan["validation_warnings"])


def test_candidate_delta_ignores_layout_churn_but_keeps_real_evidence_changes():
    layout_churn = rs.candidate_delta_summary({
        "rewritten_bullets": [{
            "source_id": "experience:cie:b1",
            "source_text": "Chosen to lead healthcare research, receiving an $8,000 fellowship to develop a wearable posture-monitoring prototype",
            "final_text": "Chosen to lead healthcare research; received an $8,000 fellowship for a wearable posture-monitoring prototype",
            "source_ids": ["experience:cie:b1"],
            "added_supported_terms": [], "dropped_supported_terms": [],
        }],
        "keyword_coverage": {"gained_terms": [], "lost_terms": []},
        "project_swaps": {"swapped_in": [], "swapped_out": []},
        "front_matter_rewrites": [], "removed_front_matter": [],
        "experience_order": {"changed": False},
    })
    assert layout_churn["status"] == "unchanged"

    material = rs.candidate_delta_summary({
        "added_bullets": [{"source_id": "project:rest:b1"}],
        "keyword_coverage": {"gained_terms": ["REST"], "lost_terms": []},
    })
    assert material["material"] is True
    assert "new source-backed evidence line(s)" in material["reasons"]


def test_provider_policy_pins_luna_to_codex_model_not_a_provider_lane(monkeypatch):
    monkeypatch.setattr(rs.shutil, "which", lambda name: "/usr/bin/" + name)
    commands = rs.provider_commands()
    assert set(commands) == {"codex"}
    assert "luna" not in commands
    assert rs.CODEX_LUNA_MODEL == "gpt-5.6-luna"


def test_codex_effort_profile_spends_depth_on_writing_and_speed_on_mechanics(monkeypatch):
    monkeypatch.delenv("RESUME_STUDIO_CODEX_EFFORT", raising=False)
    monkeypatch.delenv("RESUME_STUDIO_DRAFT_CODEX_EFFORT", raising=False)
    monkeypatch.delenv("RESUME_STUDIO_LINE_EDIT_CODEX_EFFORT", raising=False)
    assert rs.codex_effort_task("draft") == "draft"
    assert rs.codex_effort_task("critique_evidence") == "review"
    assert rs.codex_effort_task("revision_critique_technical") == "review"
    assert rs.codex_effort_task("line_edit_2") == "line_edit"
    assert rs.codex_reasoning_effort("draft") == "high"
    assert rs.codex_reasoning_effort("space_expansion") == "high"
    assert rs.codex_reasoning_effort("line_edit") == "high"
    assert rs.codex_reasoning_effort("revision_critique_technical", override=rs.CODEX_RECHECK_EFFORT) == "high"
    monkeypatch.setenv("RESUME_STUDIO_CODEX_EFFORT", "max")
    assert rs.codex_reasoning_effort("draft") == "max"
    monkeypatch.setenv("RESUME_STUDIO_LINE_EDIT_CODEX_EFFORT", "low")
    assert rs.codex_reasoning_effort("line_edit") == "max"
    assert rs.codex_reasoning_effort("draft", override="max") == "max"


def test_provider_error_result_is_not_usable():
    assert not rs.useful_provider_data({"is_error": True}, "draft")


def test_role_labeled_critic_response_is_usable():
    data = {"criteria": {
        name: {"status": "pass", "reason": "checked"}
        for name in rs.REVIEW_CRITERIA
    }}
    assert rs.useful_provider_data(data, "critique_evidence")
    assert rs.useful_provider_data(data, "revision_critique_screening")


def test_provider_usage_tokens_reads_codex_footer(tmp_path):
    stderr = tmp_path / "stderr.txt"
    stderr.write_text("codex\ntokens used\n51,191\n")
    assert rs.provider_usage_tokens(stderr) == 51191


def test_prompt_permanently_excludes_ticc_from_every_target():
    catalog = _fixture_catalog()
    jnj = rs.base_prompt({"company": "Johnson & Johnson"}, "editor", catalog, False)
    bms = rs.base_prompt({"company": "Bristol Myers Squibb"}, "editor", catalog, True)
    assert "TICC is permanently excluded" in jnj
    assert "TICC is permanently excluded" in bms
    assert "Never return a LaTeX document" in jnj


def test_generation_prompt_exposes_compact_supported_skills_checklist():
    prompt = rs.base_prompt(
        {
            "company": "Anduril",
            "generation_strategy": {
                "requirements": [{
                    "requirement": "Software-engineering fundamentals",
                    "exact_terms": ["testing", "version control"],
                    "evidence_status": "direct",
                    "evidence_ids": ["doc:testing"],
                    "recommended_action": "tailor_skills",
                }, {
                    "requirement": "Clearance",
                    "exact_terms": ["clearance"],
                    "evidence_status": "unsupported",
                    "evidence_ids": [],
                    "recommended_action": "leave_gap",
                }],
            },
        },
        "editor",
        _fixture_catalog(),
        True,
        generation=True,
    )
    assert "Short supported-skills checklist" in prompt
    assert "version control" in prompt
    assert '"doc:testing"' in prompt
    assert '"clearance"' not in prompt.split("Short supported-skills checklist", 1)[1]


def test_ticc_bullet_is_rejected_by_source_addressed_validation():
    catalog = _fixture_catalog()
    graph = {
        "nodes": [
            {"id": bullet["id"], "claim_allowed": True}
            for entry in catalog["entries"].values()
            for bullet in entry["bullets"]
        ]
    }
    plan = _fixture_plan()
    plan["experiences"][0]["bullets"][0].update({
        "text": "TICC member",
        "evidence_ids": ["experience:item0:b1"],
        "source_ids": ["experience:item0:b1"],
    })
    _, errors = rs.validate_plan(plan, catalog, enhance=True, graph=graph)
    assert any("permanently excluded" in error for error in errors)


def test_reviewer_prompt_is_role_separated_and_does_not_sparsify_by_rule():
    prompt = rs.reviewer_prompt(
        {"company": "Mayo Clinic"},
        "proposed tex",
        plan=_fixture_plan(),
        graph_context=[],
        catalog=_fixture_catalog(),
    )
    assert "Codex Luna multi-role critic jury" in prompt
    assert "same-model role-separated review" in prompt
    assert "Do not return a replacement plan" in prompt
    assert "Sections and bullet counts are adaptive" in prompt


def test_gate_report_never_calls_an_unavailable_panel_review_ready():
    critique = {"provider": "codex", "data": {
        "criteria": {name: {"status": "pass", "reason": "ok"} for name in rs.REVIEW_CRITERIA},
        "blocking_issues": [], "unsupported_claims": [], "missing_evidence": [],
        "revision_priorities": [], "line_feedback": [],
    }}
    deterministic = {"gates": {
        "factual": {"status": "pass", "reason": "ok"},
        "layout": {"status": "pass", "reason": "ok"},
        "portfolio": {"status": "pass", "reason": "ok"},
    }}
    result = rs.score_review(critique, deterministic, independent_available=False)
    assert result["ready"] is False
    assert result["gates"]["critic_jury"]["status"] == "fail"
    assert result["gates"]["independent_review"]["status"] == "fail"


def test_codex_luna_jury_is_ready_without_claiming_vendor_independence():
    critique = {"provider": "codex", "data": {
        "criteria": {name: {"status": "pass", "reason": "role passed"} for name in rs.REVIEW_CRITERIA},
        "blocking_issues": [], "unsupported_claims": [], "missing_evidence": [],
        "revision_priorities": [], "line_feedback": [],
        "portfolio_comparison": {
            "status": "pass", "reason": "complementary evidence",
            "preserved_strengths": ["systems evidence"],
            "gained_strengths": ["healthcare software relevance"],
            "lost_strengths": [],
        },
    }}
    deterministic = {"gates": {
        "factual": {"status": "pass", "reason": "ok"},
        "layout": {"status": "pass", "reason": "ok"},
        "portfolio": {"status": "pass", "reason": "ok"},
        "eligibility": {"status": "pass", "reason": "ok"},
    }}
    roles = [item["key"] for item in rs.CODEX_CRITIC_ROLES]
    result = rs.score_review(
        critique, deterministic, independent_available=True,
        review_mode=rs.CODEX_REVIEW_MODE, critic_roles=roles,
    )
    assert result["ready"] is True
    assert result["gates"]["critic_jury"]["status"] == "pass"
    assert result["gates"]["independent_review"]["status"] == "partial"
    assert result["critic_jury"] == {
        "available": True,
        "mode": rs.CODEX_REVIEW_MODE,
        "roles": roles,
        "separate_vendor": False,
    }
    assert "independent_review" not in {gate["name"] for gate in rs.build_tailoring_audit(
        {"company": "Example"},
        {"posting_text": "A real posting " * 100, "job_intelligence": {"posting_available": True}},
        {"score": 80, "confidence": "high"},
        {"hash": "graph"},
        {"experiences": [], "projects": [], "leadership": []},
        {"rewritten_bullets": [], "added_bullets": [], "removed_canonical_bullets": [], "portfolio_diagnostics": {}},
        {"gates": deterministic["gates"]},
        result,
        "base", "tailored",
    )["hard_gates"]}


def test_wrapped_enhancement_restores_approved_source_text():
    catalog = _fixture_catalog()
    plan, errors = rs.validate_plan(_fixture_plan(), catalog, enhance=False)
    assert not errors
    bullet = plan["projects"][0]["bullets"][0]
    bullet["text"] = "An overlong enhanced rewrite that wrapped"
    layout = {
        "horizontal": {
            "bullets": [{"source_id": bullet["source_id"], "wraps": True}]
        }
    }
    restored, restored_ids = rs.restore_wrapped_source_text(plan, layout, catalog)
    assert restored_ids == [bullet["source_id"]]
    assert restored["projects"][0]["bullets"][0]["text"] == catalog["entries"][
        "project:item0"
    ]["bullets"][0]["text"]


def test_job_search_and_sort(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "jobs.json").write_text(
        json.dumps(
            {
                "a": {"id": "a", "company": "Other", "title": "SWE", "score": 90, "alert_ok": False},
                "b": {"id": "b", "company": "Johnson & Johnson", "title": "TLDP", "score": 70, "alert_ok": True},
            }
        )
    )
    assert rs.list_jobs(tmp_path, query="johnson")[0]["id"] == "b"
    assert rs.list_jobs(tmp_path)[0]["id"] == "b"


def test_current_scored_jobs_uses_persisted_projection_when_current(tmp_path, monkeypatch):
    from radar.score import RULES_VERSION

    state = tmp_path / "state"
    state.mkdir()
    (state / "jobs.json").write_text(json.dumps({
        "job-1": {
            "id": "job-1", "company": "Acme", "title": "Software Engineer",
            "score": 91, "rules_v": RULES_VERSION,
            "score_version": RULES_VERSION,
        },
    }))
    monkeypatch.setattr(
        rs.copy, "deepcopy",
        lambda value: (_ for _ in ()).throw(AssertionError("cold projection rebuilt")),
    )

    jobs = rs.current_scored_jobs(tmp_path)

    assert jobs["job-1"]["score"] == 91


def test_fetch_job_description_uses_spa_reader(monkeypatch):
    from radar import quality

    posting = "Required: Python and deep learning inference systems. " * 12
    monkeypatch.setattr(quality, "spa_kind", lambda job: "workday")
    monkeypatch.setattr(quality, "fetch_posting_spa", lambda job: (True, posting))
    monkeypatch.setattr(rs.requests, "get", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("generic GET used")))
    result = rs.fetch_job_description({"url": "https://nvidia.wd5.myworkdayjobs.com/site/job/x"})
    assert "deep learning inference" in result


def test_score_review_returns_separate_gates_without_a_composite_score():
    agent = {
        "provider": "codex",
        "data": {
            "criteria": {
                "factual": {"status": "pass"},
                "target_fit": {"status": "partial"},
                "evidence": {"status": "pass"},
                "distinctiveness": {"status": "pass"},
                "clarity": {"status": "pass"},
                "privacy": {"status": "pass"},
            },
            "unsupported_claims": [],
        },
    }
    deterministic = {
        "hard_fail": False,
        "layout": {
            "compiled": True, "pages": 1, "overfull": False, "density_pass": True,
            "horizontal": {"pass": True}, "vertical_capacity": {"pass": True},
        },
        "style": {"passed": True},
        "gates": {
            "layout": {"status": "pass", "reason": "verified"},
            "eligibility": {"status": "pass", "reason": "verified"},
        },
    }
    result = rs.score_review(agent, deterministic, independent_available=True)
    assert result["craft_score"] is None
    assert result["ready"] is False
    assert result["score"] is None
    assert result["gates"]["factual"]["status"] == "pass"
    assert result["gates"]["target_fit"]["status"] == "partial"
    assert result["decision_feedback"] == []


def test_critic_issue_panel_dedupes_consensus_and_separates_fit_gaps():
    records = [
        {"critic_role": "evidence", "data": {"blocking_issues": [
            "Automated testing is not demonstrated and no authorized evidence supports it.",
            "The rendered snapshot reports three near-wraps below the one-line safety threshold.",
        ]}},
        {"critic_role": "technical", "data": {"blocking_issues": [
            "The resume does not demonstrate automated testing at the level requested.",
            "The rendered page has near-wrap bullets and a material readability risk.",
        ]}},
    ]

    assessments = rs.collapse_critic_issues(records)

    fit_gap = next(item for item in assessments if item["kind"] == "candidate_fit_gap")
    layout = next(item for item in assessments if item["kind"] == "hard_blocker")
    assert fit_gap["classification"] == "QUESTIONABLE"
    assert fit_gap["support_count"] == 2
    assert layout["classification"] == "BLOCKER"
    assert layout["support_count"] == 2
    assert len(assessments) == 2
    assert rs.classify_critic_issue(
        "The resume does not visibly establish stakeholder collaboration."
    ) == "candidate_fit_gap"


def test_critic_issue_panel_clusters_variable_wording_by_underlying_loss_family():
    records = [
        {"critic_role": "evidence", "data": {"blocking_issues": [
            "The tailoring removes the $8,000 fellowship/selected-to-lead signal and the concrete 6-table SQLite architecture.",
            "Multi-Agent and Emotion overlap the J&J AI, backend, retrieval, and integration stories.",
        ]}},
        {"critic_role": "recruiter", "data": {"blocking_issues": [
            "The posture section no longer communicates its $8,000 fellowship selection or prototype qualifier.",
            "Multi-Agent Workspace repeats the J&J agent/RAG/retrieval/FastAPI story.",
        ]}},
        {"critic_role": "screening", "data": {"blocking_issues": [
            "The tailored version removes the $8,000 fellowship/selection evidence without an equivalent distinction.",
            "The selected portfolio retains a high-overlap All-NBA project.",
        ]}},
    ]

    assessments = rs.collapse_critic_issues(records)

    assert len(assessments) == 2
    external = next(item for item in assessments if "fellowship" in item["issue"].lower())
    overlap = next(item for item in assessments if "overlap" in item["issue"].lower() or "repeats" in item["issue"].lower())
    assert external["support_count"] == 3
    assert external["agreement"] == "consensus"
    assert overlap["support_count"] == 3
    assert overlap["agreement"] == "consensus"


def test_single_role_tailoring_regression_stays_reviewable_not_conclusive():
    findings = rs.build_change_findings(
        {}, {}, {
            "critic_jury": {"available": True},
            "blocking_issue_assessments": [{
                "issue": "The tailored version removed a strong quantified proof point.",
                "kind": "tailoring_regression",
                "support_count": 1,
                "supporting_roles": ["recruiter"],
                "agreement": "single_role",
            }],
        },
    )
    assert len(findings) == 1
    assert findings[0]["classification"] == "QUESTIONABLE"
    assert findings[0]["critic_consensus"]["support_count"] == 1
    assert "Single-critic concern" in findings[0]["reason"]


def test_score_review_does_not_turn_a_fit_gap_into_a_readiness_failure():
    criteria = {name: {"status": "pass", "reason": "checked"} for name in rs.REVIEW_CRITERIA}
    criteria["target_fit"] = {"status": "partial", "reason": "testing is not evidenced"}
    agent = {"provider": "codex", "data": {
        "criteria": criteria,
        "blocking_issues": ["Automated testing is not demonstrated; no authorized evidence supports it."],
        "blocking_issue_assessments": [{
            "issue": "Automated testing is not demonstrated; no authorized evidence supports it.",
            "kind": "candidate_fit_gap", "classification": "QUESTIONABLE", "severity": "warning",
            "supporting_roles": ["technical"], "support_count": 1,
        }],
        "unsupported_claims": [],
        "portfolio_comparison": {
            "status": "pass", "reason": "comparison checked",
            "preserved_strengths": [], "gained_strengths": [], "lost_strengths": [],
        },
    }}
    deterministic = {"gates": {
        "factual": {"status": "pass", "reason": "ok"},
        "layout": {"status": "pass", "reason": "ok"},
        "portfolio": {"status": "pass", "reason": "ok"},
        "eligibility": {"status": "pass", "reason": "ok"},
    }}

    result = rs.score_review(
        agent, deterministic, independent_available=True,
        review_mode=rs.CODEX_REVIEW_MODE,
        critic_roles=[item["key"] for item in rs.CODEX_CRITIC_ROLES],
    )

    assert result["gates"]["target_fit"]["status"] == "partial"
    assert result["hard_fail"] is False
    assert result["ready"] is True
    assert result["fit_gaps"][0]["kind"] == "candidate_fit_gap"


def test_score_review_hard_fails_unsupported_claims():
    agent = {"provider": "codex", "data": {"criteria": {}, "unsupported_claims": ["invented users"]}}
    result = rs.score_review(
        agent,
        {
            "hard_fail": False,
            "layout": {
                "compiled": True, "pages": 1, "overfull": False, "density_pass": True,
                "horizontal": {"pass": True}, "vertical_capacity": {"pass": True},
            },
            "style": {"passed": True},
            "gates": {
                "layout": {"status": "pass", "reason": "verified"},
                "eligibility": {"status": "pass", "reason": "verified"},
            },
        },
    )
    assert result["hard_fail"] is True
    assert result["ready"] is False


def test_score_review_ignores_explicit_no_claim_statement():
    criteria = {name: {"status": "pass", "reason": "checked"} for name in rs.REVIEW_CRITERIA}
    result = rs.score_review(
        {"provider": "codex", "data": {
            "criteria": criteria,
            "unsupported_claims": [
                "No unsupported or exaggerated tailored-resume bullet was identified. "
                "Unit testing is an unsupported target term, not a claim made in the resume."
            ],
            "portfolio_comparison": {
                "status": "pass", "reason": "checked",
                "preserved_strengths": [], "gained_strengths": [], "lost_strengths": [],
            },
        }},
        {"gates": {
            "factual": {"status": "pass", "reason": "checked"},
            "layout": {"status": "pass", "reason": "checked"},
            "portfolio": {"status": "pass", "reason": "checked"},
            "eligibility": {"status": "pass", "reason": "checked"},
        }},
        independent_available=True,
        review_mode=rs.CODEX_REVIEW_MODE,
        critic_roles=[item["key"] for item in rs.CODEX_CRITIC_ROLES],
    )
    assert result["hard_fail"] is False
    assert result["gates"]["factual"]["status"] == "pass"
    assert result["unsupported_claims"] == []
    assert len(result["ignored_unsupported_claims"]) == 1


def test_score_review_cannot_average_away_failed_eligibility_gate():
    agent = {"provider": "codex", "data": {
        "criteria": {name: {"status": "pass", "reason": "ok"} for name in rs.REVIEW_CRITERIA},
        "portfolio_comparison": {"status": "pass", "reason": "better", "preserved_strengths": [], "gained_strengths": [], "lost_strengths": []},
        "unsupported_claims": [], "blocking_issues": [],
    }}
    deterministic = {"gates": {
        "factual": {"status": "pass", "reason": "ok"},
        "layout": {"status": "pass", "reason": "ok"},
        "portfolio": {"status": "pass", "reason": "ok"},
        "eligibility": {"status": "fail", "reason": "PhD required"},
    }}
    result = rs.score_review(agent, deterministic, independent_available=True)
    assert result["gates"]["eligibility"]["status"] == "fail"
    assert result["ready"] is False
    assert result["hard_fail"] is True


def test_deterministic_review_fails_full_posting_degree_gate(monkeypatch):
    monkeypatch.setattr(rs, "template_style_guard", lambda tex, root: {"passed": True})
    layout = {
        "compiled": True, "pages": 1, "overfull": False, "density_pass": True,
        "horizontal": {"pass": True}, "vertical_capacity": {"pass": True}, "warnings": [],
    }
    result = rs.deterministic_review(
        {
            "company": "NVIDIA", "alert_ok": True,
            "posting_text": "Completing or recently completed a Ph.D. in Computer Science is required.",
        },
        "Victor Jimenez vmj@njit.edu",
        layout,
    )
    assert result["gates"]["eligibility"]["status"] == "fail"
    assert "PhD" in result["gates"]["eligibility"]["reason"]


def test_artifact_path_cannot_escape_run_dir(tmp_path):
    run_dir = tmp_path / "runs" / "abc"
    run_dir.mkdir(parents=True)
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    target = (run_dir / ".." / ".." / "secret.txt").resolve()
    assert run_dir.resolve() not in target.parents
