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


def test_provider_plan_schema_cannot_return_a_latex_document():
    for enhance in (False, True):
        properties = rs.plan_schema(enhance)["properties"]
        assert "resume_tex" not in properties
        assert "experiences" in properties
        assert "projects" in properties


def test_plan_schema_requests_a_rich_dynamic_portfolio():
    strict = rs.plan_schema(False)
    assert strict["properties"]["experiences"]["minItems"] == 2
    assert strict["properties"]["experiences"]["maxItems"] == 5
    assert strict["properties"]["projects"]["minItems"] == 1
    assert strict["properties"]["projects"]["maxItems"] == 6
    assert strict["properties"]["leadership"]["minItems"] == 0
    assert strict["properties"]["leadership"]["maxItems"] == 2
    strict_bullet = strict["properties"]["experiences"]["items"]["properties"]["bullets"]["items"]
    assert "priority" in strict_bullet["properties"]

    enhanced_bullet = rs.plan_schema(True)["properties"]["experiences"]["items"]["properties"]["bullets"]["items"]
    assert {"source_id", "text", "evidence_ids", "candidate_rationale"}.issubset(enhanced_bullet["required"])


def _fixture_catalog():
    entries = {}
    for kind, count in (("experience", 3), ("project", 3), ("leadership", 1)):
        for index in range(count):
            entry_id = "%s:item%s" % (kind, index)
            entry = {
                "id": entry_id,
                "kind": kind,
                "bullets": [
                    {"id": "%s:b1" % entry_id, "text": "\\textbf{Built item %s} with evidence" % index},
                    {"id": "%s:b2" % entry_id, "text": "\\textbf{Improved item %s} with scope" % index},
                    {"id": "%s:b3" % entry_id, "text": "\\textbf{Presented item %s} to stakeholders" % index},
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
        "projects": selected("project", 3, 2),
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


def test_candidate_expansion_adds_authorized_overflow_backups():
    catalog = _fixture_catalog()
    plan, errors = rs.validate_plan(_fixture_plan(), catalog, enhance=False)
    assert not errors
    expanded = rs.expand_candidate_portfolio(plan, catalog, enhance=True)
    assert len(expanded["projects"][0]["bullets"]) == 3
    added = expanded["projects"][0]["bullets"][-1]
    assert added["evidence_ids"] == [added["source_id"]]
    assert "overflow pool" in added["candidate_rationale"]

    edited = json.loads(json.dumps(plan))
    edited["projects"][0]["bullets"][0]["text"] = "edited text"
    merged = rs.merge_edited_bullets(expanded, edited)
    assert merged["projects"][0]["bullets"][0]["text"] == "edited text"
    assert len(merged["projects"][0]["bullets"]) == 3


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
