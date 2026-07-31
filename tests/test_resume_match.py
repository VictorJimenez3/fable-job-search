import json

from radar.resume_match import (MATCH_VERSION, build_evidence_graph,
                                evidence_context, job_match_hash,
                                posting_eligibility_blocks,
                                score_resume_match)


def _catalog():
    return {
        "entries": {
            "experience:jnj": {
                "id": "experience:jnj",
                "kind": "experience",
                "company": "Johnson & Johnson",
                "role": "AI / Data Science Intern",
                "bullets": [
                    {
                        "id": "experience:jnj:b1",
                        "source": "resume.tex",
                        "text": "Architected agentic LLM and RAG platform on Google Cloud with FastAPI and AlloyDB",
                    }
                ],
            },
            "project:vision": {
                "id": "project:vision",
                "kind": "project",
                "heading": "Emotion AI",
                "bullets": [
                    {
                        "id": "project:vision:b1",
                        "source": "cv_full.tex",
                        "text": "Won first place after engineering a computer vision pipeline tracking seven emotions",
                    }
                ],
            },
        }
    }


def test_evidence_graph_keeps_authority_and_public_boundary(tmp_path):
    cv = tmp_path / "CV"
    studio = cv / ".resume_studio"
    (cv / "experiences").mkdir(parents=True)
    (cv / "experiences" / "JJ_SOURCE_OF_TRUTH.md").write_text(
        "# Confirmed\n\nLed a 3-person team building grounded drug-safety retrieval on Google Cloud.\n"
    )
    studio.mkdir()
    (studio / "public_portfolio_sources.json").write_text(json.dumps({
        "fetched_at": 1,
        "records": [{
            "id": "github:x", "source": "https://github.com/x", "heading": "Repo",
            "text": "A public machine learning repository", "authority": 50,
            "claim_allowed": False, "source_kind": "public corroboration", "tokens": ["machine", "learning"],
        }],
    }))
    graph = build_evidence_graph(cv, studio, _catalog())
    source_truth = next(node for node in graph["nodes"] if node["source"].endswith("JJ_SOURCE_OF_TRUTH.md"))
    public = next(node for node in graph["nodes"] if node["id"] == "github:x")
    assert source_truth["authority"] == 100 and source_truth["claim_allowed"] is True
    assert public["claim_allowed"] is False
    assert graph["version"] == MATCH_VERSION


def test_resume_match_is_fixed_source_grounded_and_explained(tmp_path):
    cv = tmp_path / "CV"
    studio = cv / ".resume_studio"
    cv.mkdir()
    studio.mkdir()
    graph = build_evidence_graph(cv, studio, _catalog())
    job = {
        "id": "job-1", "company": "Health AI", "title": "Machine Learning Platform Engineer, New Grad",
        "sector": "healthtech", "alert_ok": True,
    }
    posting = (
        "Required qualifications include Python, machine learning, cloud APIs, SQL databases, and healthcare data. "
        "Preferred qualifications include RAG, FastAPI, and computer vision."
    )
    result = score_resume_match(job, graph, posting)
    assert 75 <= result["score"] <= 100
    assert result["confidence"] == "low"
    assert result["dimensions"]["required_coverage"] > 0
    assert result["sources"]
    assert result["version"] == MATCH_VERSION
    assert job_match_hash(job, posting) != job_match_hash(job, "")


def test_evidence_context_prefers_target_relevance(tmp_path):
    cv = tmp_path / "CV"
    studio = cv / ".resume_studio"
    cv.mkdir()
    studio.mkdir()
    graph = build_evidence_graph(cv, studio, _catalog())
    context = evidence_context(graph, {"title": "Computer Vision Engineer", "sector": "healthtech"}, limit=1)
    assert context[0]["id"] == "project:vision:b1"


def test_full_posting_eligibility_cannot_be_offset_by_match_strength(tmp_path):
    cv = tmp_path / "CV"
    studio = cv / ".resume_studio"
    cv.mkdir()
    studio.mkdir()
    graph = build_evidence_graph(cv, studio, _catalog())
    job = {
        "id": "job-phd", "company": "GPU Co",
        "title": "Research Scientist, Deep Learning - New College Grad",
        "sector": "big_tech", "alert_ok": True,
    }
    posting = (
        "What we need to see: Completing or recently completed a Ph.D. in Computer Science, "
        "or equivalent research experience. Python and PyTorch are required."
    )
    result = score_resume_match(job, graph, posting)
    assert posting_eligibility_blocks(posting) == ["PhD or equivalent research experience required"]
    assert result["dimensions"]["eligibility"] == 0
    assert any(item.startswith("eligibility:") for item in result["missing_requirements"])
