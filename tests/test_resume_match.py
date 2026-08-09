import base64
import hashlib
import json

import requests

from radar.resume_match import (MATCH_VERSION, build_evidence_graph,
                                evidence_context, job_match_hash,
                                posting_eligibility_blocks,
                                refresh_public_sources, score_resume_match)
from radar.evidence_review import apply_reviews


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


def test_public_refresh_includes_repository_readme_as_corroboration(tmp_path):
    class Response:
        def __init__(self, payload=None, text=""):
            self.payload = payload
            self.text = text

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class Session:
        def get(self, url, **kwargs):
            if url.endswith("/users/VictorJimenez3/repos"):
                return Response([{
                    "name": "demo-project",
                    "description": "A FastAPI machine learning project",
                    "topics": ["python"],
                    "language": "Python",
                    "html_url": "https://github.com/VictorJimenez3/demo-project",
                }])
            if url.endswith("/repos/VictorJimenez3/demo-project/readme"):
                encoded = base64.b64encode(
                    b"# Demo\nBuilt a modular FastAPI pipeline with SQL retrieval."
                ).decode("ascii")
                return Response({
                    "content": encoded,
                    "html_url": "https://github.com/VictorJimenez3/demo-project/blob/main/README.md",
                })
            return Response({}, "<html>Devpost</html>")

    studio = tmp_path / "CV" / ".resume_studio"
    studio.mkdir(parents=True)
    payload = refresh_public_sources(studio, force=True, session=Session())
    readme = next(item for item in payload["records"] if item["heading"] == "demo-project README")
    assert readme["source_kind"] == "public repository README"
    assert readme["claim_allowed"] is False
    assert "FastAPI" in readme["text"]


def test_public_refresh_keeps_cached_readme_when_github_rate_limited(tmp_path):
    class Response:
        def __init__(self, payload=None, status_code=200):
            self.payload = payload
            self.status_code = status_code
            self.text = payload if isinstance(payload, str) else ""

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.RequestException("rate limit exceeded")

        def json(self):
            return self.payload

    class Session:
        def get(self, url, **kwargs):
            if url.endswith("/users/VictorJimenez3/repos"):
                return Response([{
                    "name": "demo-project",
                    "description": "A demo project",
                    "topics": [],
                    "language": "Python",
                    "html_url": "https://github.com/VictorJimenez3/demo-project",
                }])
            if url.endswith("/repos/VictorJimenez3/demo-project/readme"):
                return Response(status_code=403)
            return Response("<html>Devpost</html>")

    studio = tmp_path / "CV" / ".resume_studio"
    studio.mkdir(parents=True)
    repo_url = "https://github.com/VictorJimenez3/demo-project"
    cached = {
        "fetched_at": 1,
        "records": [{
            "id": "github:cached",
            "source": repo_url,
            "heading": "demo-project",
            "text": "demo-project Python",
            "authority": 50,
            "claim_allowed": False,
            "source_kind": "public corroboration",
            "tokens": ["python"],
        }, {
            "id": "github-readme:" + hashlib.sha256(repo_url.encode()).hexdigest()[:12],
            "source": "https://github.com/VictorJimenez3/demo-project/blob/main/README.md",
            "heading": "demo-project README",
            "text": "Built a modular FastAPI pipeline with SQL retrieval.",
            "authority": 50,
            "claim_allowed": False,
            "source_kind": "public repository README",
            "tokens": ["fastapi", "sql"],
        }],
    }
    (studio / "public_portfolio_sources.json").write_text(json.dumps(cached))
    payload = refresh_public_sources(studio, force=True, session=Session())
    readme = next(item for item in payload["records"] if item["heading"] == "demo-project README")
    assert "FastAPI" in readme["text"]
    assert payload["refresh_errors"] == []
    assert payload["refresh_warnings"]




def test_rejected_evidence_is_removed_from_ranked_context(tmp_path):
    cv = tmp_path / "CV"
    studio = cv / ".resume_studio"
    cv.mkdir()
    studio.mkdir()
    graph = build_evidence_graph(cv, studio, _catalog())
    target = next(node for node in graph["nodes"] if node.get("entry_id") == "project:vision")
    (studio / "evidence_review.json").write_text(json.dumps({
        "version": "evidence-review-v1",
        "claims": {target["id"]: {
            "status": "rejected", "note": "User rejected this claim."
        }},
    }))
    reviewed = build_evidence_graph(cv, studio, _catalog())
    rejected = next(node for node in reviewed["nodes"] if node["id"] == target["id"])
    assert rejected["review_status"] == "rejected"
    assert rejected["claim_allowed"] is False
    assert all(node["id"] != target["id"] for node in evidence_context(
        reviewed, {"title": "Computer Vision Engineer", "sector": "healthtech"}, limit=20
    ))


def test_public_safe_promotes_only_explicitly_allowed_claims_and_disputed_blocks():
    graph = {"nodes": [{"id": "doc:one", "claim_allowed": False}]}
    reviewed = apply_reviews(graph, {"claims": {
        "doc:one": {"status": "public_safe", "claim_allowed": True}
    }})
    assert reviewed["nodes"][0]["claim_allowed"] is True
    disputed = apply_reviews(reviewed, {"claims": {
        "doc:one": {"status": "disputed", "note": "Needs owner verification."}
    }})
    assert disputed["nodes"][0]["claim_allowed"] is False
