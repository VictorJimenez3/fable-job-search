import json
from pathlib import Path

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


def test_prompt_applies_ticc_rule_only_to_johnson_context():
    catalog = _fixture_catalog()
    jnj = rs.base_prompt({"company": "Johnson & Johnson"}, "editor", catalog, False)
    bms = rs.base_prompt({"company": "Bristol Myers Squibb"}, "editor", catalog, True)
    assert "TICC is not a priority" in jnj
    assert "do not apply Johnson & Johnson-specific exclusions" in bms
    assert "Never return a LaTeX document" in jnj


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
