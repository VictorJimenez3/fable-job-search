import json
from pathlib import Path

import pytest

from scripts import resume_studio as rs


def test_extract_json_handles_provider_wrapper_and_fences():
    raw = json.dumps({"result": "```json\n{\"resume_tex\": \"hello\"}\n```"})
    assert rs.extract_json(raw) == {"resume_tex": "hello"}


def test_response_data_preserves_structured_plan():
    raw = json.dumps({"experiences": [{"source_id": "experience:a"}], "projects": []})
    data = rs.response_data(raw)
    assert data["experiences"][0]["source_id"] == "experience:a"


def test_write_json_uses_an_atomic_worker_specific_temp_file(tmp_path):
    target = tmp_path / "value.json"
    rs.write_json(target, {"ok": True})
    assert json.loads(target.read_text()) == {"ok": True}
    assert list(tmp_path.glob("*.tmp")) == []


def test_generated_resume_filename_is_company_identifiable():
    assert rs.resume_pdf_filename({"company": "Johnson & Johnson"}) == "johnson_johnson_resume_ai.pdf"
    assert rs.resume_pdf_filename({"company": "NVIDIA"}) == "nvidia_resume_ai.pdf"


def test_project_heading_uses_pipe_separator():
    heading = r"\\textbf{PostureMax --- 1st Place Overall} | \\emph{Python, Flask}"
    assert rs._project_heading(heading) == r"\\textbf{PostureMax | 1st Place Overall} | \\emph{Python, Flask}"


def test_pdf_preview_download_name_is_safe_and_company_identifiable():
    assert rs._download_filename("mayo_clinic_rochester_resume_ai.pdf") == "mayo_clinic_rochester_resume_ai.pdf"
    assert rs._download_filename("../../resume.pdf") == "resume.pdf"
    assert rs._download_filename("Mayo Clinic resume.pdf") == "Mayo_Clinic_resume.pdf"


def test_resume_report_exposes_change_and_layout_safety_language():
    assert "What changed" in rs.UI_HTML
    assert "rewritten lines" in rs.UI_HTML
    assert "Supported but missing" in rs.UI_HTML
    assert "near-wraps" in rs.UI_HTML
    assert "roomy lines" in rs.UI_HTML
    assert "layout.horizontal.near_wrap_count" in rs.UI_HTML


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
    rs.write_json(run / "report.json", {"mode": "enhanced", "job": rs.job_summary(job), "review": {"craft_score": 88}})
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
    snapshot = rs.posting_snapshot(tmp_path, "run", "0123456789ab")
    assert snapshot["posting_text"] == "Required: Python and SQL."


def test_run_manager_snapshots_job_and_assigns_named_pdf(tmp_path):
    class NoopExecutor:
        def submit(self, *args, **kwargs):
            return None

    manager = rs.RunManager(tmp_path)
    manager.executor.shutdown(wait=False)
    manager.executor = NoopExecutor()
    job = {"id": "job-3", "company": "Acme Labs", "title": "ML Engineer"}
    status = manager.start(job, "dream")
    run_dir = tmp_path / "CV" / ".resume_studio" / "runs" / status["run_id"]
    assert status["pdf_filename"] == "acme_labs_resume_ai.pdf"
    assert json.loads((run_dir / "job.json").read_text())["company"] == "Acme Labs"


def test_provider_plan_schema_cannot_return_a_latex_document():
    for enhance in (False, True):
        properties = rs.plan_schema(enhance)["properties"]
        assert "resume_tex" not in properties
        assert "experiences" in properties
        assert "projects" in properties


def test_plan_schema_requests_a_full_ranked_candidate_pool():
    strict = rs.plan_schema(False)
    assert strict["properties"]["experiences"]["minItems"] == 3
    assert strict["properties"]["experiences"]["maxItems"] == 3
    assert strict["properties"]["projects"]["minItems"] == 4
    assert strict["properties"]["projects"]["maxItems"] == 5
    assert strict["properties"]["leadership"]["minItems"] == 1
    assert strict["properties"]["leadership"]["maxItems"] == 2
    strict_bullet = strict["properties"]["experiences"]["items"]["properties"]["bullets"]["items"]
    assert "priority" in strict_bullet["properties"]

    enhanced_bullet = rs.plan_schema(True)["properties"]["experiences"]["items"]["properties"]["bullets"]["items"]
    assert {"source_id", "text", "evidence_ids", "candidate_rationale"}.issubset(enhanced_bullet["required"])
    assert "source_ids" in enhanced_bullet["required"]


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


def test_review_schema_requires_a_complete_corrected_plan():
    schema = rs.reviewed_plan_schema(True)
    assert "final_plan" in schema["required"]
    final_plan = schema["properties"]["final_plan"]
    assert final_plan["properties"]["experiences"]["minItems"] == 3
    assert final_plan["properties"]["projects"]["minItems"] == 4
    assert final_plan["properties"]["leadership"]["minItems"] == 1


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
    assert rs.portfolio_metrics(expanded)["total_bullets"] == rs.MIN_TOTAL_BULLETS
    assert all(len(entry["bullets"]) == 3 for entry in expanded["projects"])


def test_candidate_expansion_adds_authorized_source_backups():
    catalog = _fixture_catalog()
    plan, errors = rs.validate_plan(_fixture_plan(), catalog, enhance=False)
    assert not errors
    plan["experiences"][0]["bullets"].pop()
    expanded = rs.expand_candidate_portfolio(plan, catalog, enhance=True)
    assert rs.portfolio_metrics(expanded)["total_bullets"] == rs.MIN_TOTAL_BULLETS
    added = expanded["experiences"][0]["bullets"][-1]
    assert added["evidence_ids"] == [added["source_id"]]
    assert "overflow pool" in added["candidate_rationale"]

    edited = json.loads(json.dumps(plan))
    edited["projects"][0]["bullets"][0]["text"] = "edited text"
    merged = rs.merge_edited_bullets(expanded, edited)
    assert merged["projects"][0]["bullets"][0]["text"] == "edited text"
    assert rs.portfolio_metrics(merged)["total_bullets"] == rs.MIN_TOTAL_BULLETS


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
    assert tex.index("\\section{Technical Skills}") < tex.index("Python, Rust")
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
    monkeypatch.setattr(rs, "provider_commands", lambda: {"codex": "/usr/bin/codex", "claude": None})
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


def test_target_priority_outweighs_generic_technical_tiebreakers():
    target_specific = {"priority": 90, "text": "Resolved biomedical version conflicts via Pandas/SQL"}
    generic = {"priority": 87, "text": "Engineered modular API cloud system architecture"}
    assert rs._bullet_value(target_specific) > rs._bullet_value(generic)


def test_human_reference_density_contract_is_not_sparse():
    assert rs.MAX_DENSITY_GAP_PT == 24.0
    assert rs.MIN_TOTAL_BULLETS == 22
    assert rs.MAX_TOTAL_BULLETS == 26


def test_curator_restores_canonical_experience_order():
    catalog = _fixture_catalog()
    plan, errors = rs.validate_plan(_fixture_plan(), catalog, enhance=False)
    assert not errors
    plan["experiences"].reverse()
    curated = rs.curate_candidate_portfolio(plan, catalog)
    assert [entry["source_id"] for entry in curated["experiences"]] == [
        "experience:item0",
        "experience:item1",
        "experience:item2",
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
    assert len(curated["projects"]) <= 4
    assert len(curated["leadership"]) <= 2
    assert all(len(entry["bullets"]) <= 6 for entry in curated["experiences"])
    assert all(len(entry["bullets"]) <= 3 for entry in curated["projects"])


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
    (cv / "resume.tex").write_text("Python SQL PyTorch\\n")
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


def test_renderer_keeps_canonical_prefix_and_company_first(tmp_path):
    cv = tmp_path / "CV"
    cv.mkdir()
    (cv / "resume.tex").write_text(
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


def test_provider_transcript_uses_final_structured_response(tmp_path):
    stdout = tmp_path / "stdout.txt"
    stderr = tmp_path / "stderr.txt"
    stdout.write_text("")
    stderr.write_text(
        "intermediate\n"
        + json.dumps({"experiences": [], "projects": []})
        + "\n"
        + json.dumps({"experiences": [{"source_id": "experience:a"}], "projects": [{"source_id": "project:a"}]})
        + "\n"
    )
    data = rs.provider_data_from_files(stdout, stderr, "draft")
    assert data["experiences"][0]["source_id"] == "experience:a"


def test_provider_error_result_is_not_usable():
    assert not rs.useful_provider_data({"is_error": True}, "draft")


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


def test_reviewer_prompt_edits_the_final_plan_without_sparsifying():
    prompt = rs.reviewer_prompt(
        {"company": "Mayo Clinic"},
        "proposed tex",
        plan=_fixture_plan(),
        graph_context=[],
        catalog=_fixture_catalog(),
    )
    assert "complete, strongest-first replacement plan" in prompt
    assert "do not solve criticism by making the page sparse" in prompt
    assert "grade the FINAL plan" in prompt


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


def test_fetch_job_description_uses_spa_reader(monkeypatch):
    from radar import quality

    posting = "Required: Python and deep learning inference systems. " * 12
    monkeypatch.setattr(quality, "spa_kind", lambda job: "workday")
    monkeypatch.setattr(quality, "fetch_posting_spa", lambda job: (True, posting))
    monkeypatch.setattr(rs.requests, "get", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("generic GET used")))
    result = rs.fetch_job_description({"url": "https://nvidia.wd5.myworkdayjobs.com/site/job/x"})
    assert "deep learning inference" in result


def test_score_review_is_calculated_by_fixed_rubric():
    agent = {
        "provider": "codex",
        "data": {
            "criteria": {
                "factual": {"status": "pass"},
                "target_fit": {"status": "partial"},
                "evidence": {"status": "pass"},
                "clarity": {"status": "pass"},
                "portfolio": {"status": "fail"},
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
    result = rs.score_review(agent, deterministic)
    assert result["craft_score"] == 65
    assert result["ready"] is False
    assert result["criteria"]["layout"]["points"] == 10
    assert result["gates"]["factual"]["status"] == "pass"


def test_score_review_hard_fails_unsupported_claims():
    agent = {"provider": "claude", "data": {"criteria": {}, "unsupported_claims": ["invented users"]}}
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
