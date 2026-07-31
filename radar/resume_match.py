"""Private, source-addressed resume evidence and job-match scoring.

This module contains no CV data. Resume Studio passes Victor's ignored CV
directory at runtime and stores every derived artifact below that private
boundary. The fixed matcher is intentionally deterministic; frontier agents
may consume its evidence, but cannot change its rubric or score.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests


MATCH_VERSION = "resume-match-v3"
PUBLIC_CACHE_SECONDS = 7 * 86400
MATCH_WEIGHTS = {
    "required_coverage": 35,
    "preferred_coverage": 20,
    "evidence_strength": 20,
    "domain_relevance": 10,
    "eligibility": 10,
    "distinctiveness": 5,
}

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in",
    "is", "it", "of", "on", "or", "our", "that", "the", "this", "to", "we",
    "will", "with", "you", "your", "role", "work", "working", "experience",
    "years", "skills", "ability", "team", "including", "using", "used",
}

CAPABILITY_CLUSTERS = {
    "ai_ml": {"ai", "ml", "machine", "learning", "pytorch", "model", "models", "inference", "neural"},
    "llm_rag": {"llm", "llms", "rag", "gemini", "agentic", "agents", "retrieval", "vector", "embedding"},
    "data_science": {"data", "pandas", "numpy", "statistics", "analytics", "analysis", "xgboost", "sklearn"},
    "software": {"software", "engineering", "python", "java", "javascript", "typescript", "api", "object", "oriented"},
    "backend_cloud": {"backend", "cloud", "fastapi", "flask", "docker", "kubernetes", "gcp", "google", "alloydb"},
    "systems": {"systems", "distributed", "infrastructure", "platform", "performance", "scale", "hpc", "slurm"},
    "databases": {"sql", "sqlite", "database", "databases", "schema", "postgres", "pgvector", "mongodb", "firebase"},
    "research": {"research", "experiments", "experimental", "publication", "symposium", "fourier", "svd"},
    "computer_vision": {"vision", "image", "images", "video", "gaze", "facial", "emotion", "opencv"},
    "hardware": {"hardware", "embedded", "sensor", "sensors", "esp32", "raspberry", "firmware", "gpu"},
    "security": {"security", "privacy", "encryption", "homomorphic", "ddos", "cybersecurity"},
    "health": {"health", "healthcare", "clinical", "patient", "patients", "medical", "biomedical", "drug", "safety"},
    "quantum": {"quantum", "qubit", "hadamard", "circuit", "circuits", "monte", "carlo"},
    "leadership": {"led", "lead", "leadership", "managed", "mentored", "coordinated", "presented", "stakeholders"},
}

DOMAIN_CLUSTER = {
    "healthtech": "health",
    "ai_lab": "ai_ml",
    "big_tech": "systems",
    "sports": "data_science",
    "video_games": "software",
    "edtech": "llm_rag",
    "fintech": "data_science",
}

TECHNICAL_REQUIREMENT_TERMS = set().union(*CAPABILITY_CLUSTERS.values()) | {
    "cuda", "parallel", "parallelization", "training", "finetuning", "tuning",
    "pruning", "quantization", "nas", "backbones", "publication", "publications",
    "publish", "compiler", "compilers", "distributed", "optimization",
}


def _tokens(value: str) -> set[str]:
    value = re.sub(r"\\[A-Za-z]+", " ", value or "")
    return {
        token for token in re.findall(r"[a-z0-9+#.]+", value.lower())
        if len(token) > 1 and token not in STOPWORDS
    }


def _sha(value: str, length: int = 16) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:length]


def _authority(path: str) -> Tuple[int, bool, str]:
    normalized = path.replace("\\", "/")
    if normalized.endswith("experiences/JJ_SOURCE_OF_TRUTH.md"):
        return 100, True, "experience source of truth"
    if "/experiences/" in "/" + normalized:
        return 95, True, "experience dossier"
    if normalized.endswith("resume.tex"):
        return 92, True, "canonical resume"
    if normalized.endswith("tldp_resume.tex"):
        return 88, True, "approved target resume"
    if normalized.endswith("cv_full.tex"):
        return 84, True, "master CV"
    if "METHODOLOGY" in normalized or "PLAYBOOK" in normalized or normalized.endswith("AGENTS.md"):
        return 80, False, "methodology"
    if normalized.endswith("RESUME_NOTES.md") or normalized.endswith("JJ_RESUME_CONTEXT.md"):
        return 78, True, "resume context"
    if "Knowledge_Base" in normalized:
        return 60, True, "historical knowledge base"
    return 70, True, "local CV document"


def _strip_markup(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"[`*_>#|]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _markdown_nodes(path: Path, cv_dir: Path) -> List[Dict[str, Any]]:
    relative = str(path.relative_to(cv_dir))
    authority, claim_allowed, source_kind = _authority(relative)
    text = path.read_text(errors="replace")
    heading = ""
    buffer: List[str] = []
    nodes: List[Dict[str, Any]] = []

    def flush() -> None:
        paragraph = _strip_markup(" ".join(buffer))
        buffer.clear()
        if len(paragraph) < 45:
            return
        node_id = "doc:%s:%s" % (_sha(relative, 10), _sha(heading + paragraph, 10))
        nodes.append({
            "id": node_id,
            "source": "CV/" + relative,
            "heading": heading,
            "text": paragraph[:1800],
            "authority": authority,
            "claim_allowed": claim_allowed,
            "source_kind": source_kind,
            "tokens": sorted(_tokens(paragraph)),
        })

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("#"):
            flush()
            heading = line.lstrip("#").strip()
        elif not line:
            flush()
        elif not line.startswith(("```", "---")):
            buffer.append(line)
    flush()
    return nodes


def _catalog_nodes(catalog: Dict[str, Any]) -> List[Dict[str, Any]]:
    nodes = []
    for entry in catalog.get("entries", {}).values():
        label = entry.get("heading") or "%s - %s" % (entry.get("company", ""), entry.get("role", ""))
        for bullet in entry.get("bullets", []):
            source = "CV/" + str(bullet.get("source") or "unknown")
            authority, claim_allowed, source_kind = _authority(str(bullet.get("source") or ""))
            text = _strip_markup(str(bullet.get("text") or ""))
            nodes.append({
                "id": str(bullet.get("id")),
                "entry_id": entry.get("id"),
                "source": source,
                "heading": _strip_markup(str(label)),
                "text": text,
                "authority": authority,
                "claim_allowed": claim_allowed,
                "source_kind": source_kind,
                "tokens": sorted(_tokens(text + " " + str(label))),
            })
    return nodes


def _public_cache_path(studio_dir: Path) -> Path:
    return studio_dir / "public_portfolio_sources.json"


def refresh_public_sources(studio_dir: Path, force: bool = False, session=requests) -> Dict[str, Any]:
    """Refresh bounded public GitHub/Devpost corroboration with a local cache."""
    path = _public_cache_path(studio_dir)
    try:
        cached = json.loads(path.read_text())
    except (OSError, ValueError, TypeError):
        cached = {}
    if not force and cached.get("fetched_at", 0) > time.time() - PUBLIC_CACHE_SECONDS:
        return cached

    records: List[Dict[str, Any]] = []
    errors: List[str] = []
    headers = {"User-Agent": "JobRadar-ResumeStudio/1.0"}
    try:
        response = session.get(
            "https://api.github.com/users/VictorJimenez3/repos",
            params={"per_page": 100, "sort": "pushed"},
            headers=headers,
            timeout=20,
        )
        response.raise_for_status()
        for repo in response.json() if isinstance(response.json(), list) else []:
            name = str(repo.get("name") or "")
            description = str(repo.get("description") or "")
            topics = " ".join(repo.get("topics") or [])
            language = str(repo.get("language") or "")
            text = _strip_markup(" ".join([name, description, topics, language]))
            if text:
                records.append({
                    "id": "github:" + _sha(str(repo.get("html_url") or name), 12),
                    "source": str(repo.get("html_url") or "https://github.com/VictorJimenez3"),
                    "heading": name,
                    "text": text,
                    "authority": 50,
                    "claim_allowed": False,
                    "source_kind": "public corroboration",
                    "tokens": sorted(_tokens(text)),
                })
    except (requests.RequestException, ValueError, TypeError) as exc:
        errors.append("GitHub refresh failed: %s" % exc)

    try:
        response = session.get("https://devpost.com/vmj", headers=headers, timeout=20)
        response.raise_for_status()
        text = _strip_markup(response.text)
        if text:
            records.append({
                "id": "devpost:vmj",
                "source": "https://devpost.com/vmj",
                "heading": "Victor Jimenez Devpost portfolio",
                "text": text[:12000],
                "authority": 55,
                "claim_allowed": False,
                "source_kind": "public corroboration",
                "tokens": sorted(_tokens(text)),
            })
    except requests.RequestException as exc:
        errors.append("Devpost refresh failed: %s" % exc)

    if not records and cached:
        cached["refresh_errors"] = errors
        return cached
    payload = {"fetched_at": int(time.time()), "records": records, "refresh_errors": errors}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)
    return payload


def build_evidence_graph(
    cv_dir: Path,
    studio_dir: Path,
    catalog: Dict[str, Any],
    refresh_public: bool = False,
) -> Dict[str, Any]:
    nodes = _catalog_nodes(catalog)
    source_fingerprints = []
    for path in sorted(cv_dir.rglob("*.md")):
        if ".resume_studio" in path.parts or ".git" in path.parts:
            continue
        source_fingerprints.append(str(path.relative_to(cv_dir)) + ":" + _sha(path.read_text(errors="replace")))
        nodes.extend(_markdown_nodes(path, cv_dir))
    if refresh_public:
        public = refresh_public_sources(studio_dir, force=True)
    else:
        try:
            public = json.loads(_public_cache_path(studio_dir).read_text())
        except (OSError, ValueError, TypeError):
            public = {"records": [], "refresh_errors": []}
    nodes.extend(public.get("records") or [])
    graph_hash = _sha(json.dumps(source_fingerprints, sort_keys=True) + json.dumps(nodes, sort_keys=True), 20)
    return {
        "version": MATCH_VERSION,
        "hash": graph_hash,
        "built_at": int(time.time()),
        "authority_order": [
            "newest explicit instruction", "experience source of truth", "methodology/playbook",
            "canonical resume", "master CV", "public corroboration",
        ],
        "nodes": nodes,
        "public_refresh_errors": public.get("refresh_errors") or [],
    }


def _cluster_hits(tokens: set[str]) -> set[str]:
    return {name for name, terms in CAPABILITY_CLUSTERS.items() if tokens & terms}


def _requirement_clusters(text: str, markers: Iterable[str]) -> set[str]:
    found: set[str] = set()
    for sentence in re.split(r"[\n.!?;]+", text or ""):
        lowered = sentence.lower()
        if any(marker in lowered for marker in markers):
            found |= _cluster_hits(_tokens(sentence))
    return found


def _requirement_terms(text: str, markers: Iterable[str]) -> set[str]:
    found: set[str] = set()
    for sentence in re.split(r"[\n.!?;]+", text or ""):
        lowered = sentence.lower()
        if any(marker in lowered for marker in markers):
            found |= _tokens(sentence) & TECHNICAL_REQUIREMENT_TERMS
    return found


def posting_eligibility_blocks(text: str) -> List[str]:
    """Candidate-level requirements that a strong portfolio cannot offset."""
    blocks: List[str] = []
    normalized_text = re.sub(r"\bph\.?\s*d\.?\b", "phd", text or "", flags=re.I)
    normalized_text = re.sub(r"\bm\.?\s*s\.?\b", "ms", normalized_text, flags=re.I)
    for sentence in re.split(r"[\n.!?;]+", normalized_text):
        lowered = sentence.lower()
        requirement_language = re.search(
            r"\b(completing|completed|pursuing|require[sd]?|must have|need to see|minimum)\b",
            lowered,
        )
        if not requirement_language:
            continue
        has_bachelors = bool(re.search(r"\b(bachelor'?s?|b\.s\.?|bs)\b", lowered))
        if re.search(r"\b(phd|doctorate)\b", lowered) and not has_bachelors:
            blocks.append("PhD or equivalent research experience required")
            continue
        if re.search(r"\b(master'?s?|m\.s\.?|ms)\b", lowered) and not has_bachelors:
            blocks.append("graduate degree or equivalent experience required")
    years = re.search(
        r"\b(?:minimum|at least|requires?|must have)?\s*(\d+)\+?\s+years?\s+(?:of\s+)?(?:relevant\s+|professional\s+|software\s+|industry\s+)?experience",
        normalized_text,
        re.I,
    )
    if years and int(years.group(1)) >= 1:
        blocks.append("%s+ years of experience required" % years.group(1))
    if re.search(r"\b(?:requires?|must have)\b[^.]{0,80}\b(?:security clearance|ts/sci|top secret)\b", normalized_text, re.I):
        blocks.append("security clearance required")
    return list(dict.fromkeys(blocks))


def _role_clusters(job: Dict[str, Any], posting_text: str) -> set[str]:
    title = str(job.get("title") or "")
    clusters = _cluster_hits(_tokens(title))
    if not clusters:
        clusters = _cluster_hits(_tokens(posting_text[:800]))
    if not clusters:
        clusters.add("software")
    return clusters


def _node_specificity(node: Dict[str, Any]) -> float:
    text = str(node.get("text") or "")
    metric = bool(re.search(r"\b\d[\d,.]*\+?%?|\$\d|\b(?:won|selected|award|grant|first place)\b", text, re.I))
    mechanism = len(_cluster_hits(set(node.get("tokens") or []))) >= 2
    return min(1.0, 0.45 + (0.3 if metric else 0) + (0.25 if mechanism else 0))


def score_resume_match(job: Dict[str, Any], graph: Dict[str, Any], posting_text: str = "") -> Dict[str, Any]:
    """Return a fixed-rubric private match score with source-level reasons."""
    runtime = graph.get("_runtime_index")
    if not isinstance(runtime, dict):
        supported = {name: [] for name in CAPABILITY_CLUSTERS}
        token_strength: Dict[str, float] = {}
        token_nodes: Dict[str, List[Dict[str, Any]]] = {}
        for node in graph.get("nodes", []):
            if not node.get("claim_allowed"):
                continue
            specificity = _node_specificity(node)
            node["_specificity"] = specificity
            strength = (int(node.get("authority") or 0) / 100) * specificity
            for token in node.get("tokens") or []:
                token_strength[str(token)] = max(token_strength.get(str(token), 0.0), strength)
                token_nodes.setdefault(str(token), []).append(node)
            node_clusters = _cluster_hits(set(node.get("tokens") or []))
            for cluster in node_clusters:
                supported[cluster].append(node)
        for cluster in supported:
            supported[cluster] = sorted(
                supported[cluster],
                key=lambda node: int(node.get("authority") or 0) * float(node.get("_specificity") or 0),
                reverse=True,
            )[:12]
        for token in token_nodes:
            token_nodes[token] = sorted(
                token_nodes[token],
                key=lambda node: int(node.get("authority") or 0) * float(node.get("_specificity") or 0),
                reverse=True,
            )[:20]
        runtime = {"supported": supported, "token_strength": token_strength, "token_nodes": token_nodes}
        graph["_runtime_index"] = runtime
    supported = runtime["supported"]

    text = posting_text or str(job.get("description") or "")
    required = _requirement_clusters(text, ("required", "must", "minimum", "qualification"))
    required_terms = _requirement_terms(text, ("required", "must", "minimum", "qualification"))
    explicit_required = bool(required)
    preferred = _requirement_clusters(text, ("preferred", "nice to have", "bonus", "ideally"))
    role_clusters = _role_clusters(job, text)
    if not required:
        required = set(role_clusters)

    matched_required = {cluster for cluster in required if supported.get(cluster)}
    matched_preferred = {cluster for cluster in preferred if supported.get(cluster)}
    if explicit_required:
        cluster_ratio = len(matched_required) / max(1, len(required))
        matched_terms = {
            token for token in required_terms
            if float(runtime["token_strength"].get(token, 0.0)) >= 0.35
        }
        if required_terms:
            term_ratio = len(matched_terms) / len(required_terms)
            required_points = round(MATCH_WEIGHTS["required_coverage"] * (0.65 * cluster_ratio + 0.35 * term_ratio))
        else:
            required_points = round(MATCH_WEIGHTS["required_coverage"] * cluster_ratio)
        direct_alignment = None
    else:
        generic_title = {"engineer", "engineering", "software", "developer", "new", "grad", "college", "associate"}
        title_tokens = _tokens(str(job.get("title") or "")) - generic_title
        token_strengths = []
        for token in title_tokens:
            token_strengths.append(float(runtime["token_strength"].get(token, 0.0)))
        direct_alignment = sum(token_strengths) / max(1, len(token_strengths))
        cluster_points = 25 * len(matched_required) / max(1, len(required))
        required_points = round(cluster_points + 10 * direct_alignment)
    preferred_points = (
        round(MATCH_WEIGHTS["preferred_coverage"] * len(matched_preferred) / len(preferred))
        if preferred else 10
    )

    relevant_clusters = required | preferred | role_clusters
    relevant_nodes = []
    for cluster in relevant_clusters:
        relevant_nodes.extend(supported.get(cluster) or [])
    target_tokens = _tokens(
        "%s %s" % (str(job.get("title") or ""), text[:5000])
    )
    # Full-posting analysis expands through the token index for source-level
    # specificity. Title-only sorting stays on the compact cluster index so
    # ranking twenty thousand roles remains interactive.
    if text:
        for token in target_tokens:
            relevant_nodes.extend((runtime.get("token_nodes") or {}).get(token) or [])
    unique_nodes = {node["id"]: node for node in relevant_nodes}.values()

    def target_rank(node: Dict[str, Any]) -> Tuple[float, int]:
        node_tokens = set(node.get("tokens") or [])
        overlap = len(target_tokens & node_tokens)
        cluster_overlap = len(relevant_clusters & _cluster_hits(node_tokens))
        authority = int(node.get("authority") or 0)
        specificity = float(node.get("_specificity") or _node_specificity(node))
        # Direct catalog bullets are actionable resume evidence. Long context
        # documents remain useful authority, but should not crowd every source
        # slot merely because they mention many generic target terms.
        actionable = 55 if node.get("entry_id") else 0
        return (
            actionable + overlap * 12 + cluster_overlap * 16 + authority * 0.35 + specificity * 20,
            len(node_tokens),
        )

    ranked_nodes = sorted(
        unique_nodes,
        key=target_rank,
        reverse=True,
    )
    actionable_nodes = [node for node in ranked_nodes if node.get("entry_id")]
    evidence_ranked = actionable_nodes or ranked_nodes
    if evidence_ranked:
        best = evidence_ranked[: min(8, len(evidence_ranked))]
        strength = sum((int(node.get("authority") or 0) / 100) * float(node.get("_specificity") or _node_specificity(node)) for node in best) / len(best)
        evidence_points = round(MATCH_WEIGHTS["evidence_strength"] * min(1.0, strength + min(len(best), 5) * 0.04))
    else:
        evidence_points = 0

    domain_cluster = DOMAIN_CLUSTER.get(str(job.get("sector") or ""))
    if domain_cluster and supported.get(domain_cluster):
        domain_points = 10
    elif set(role_clusters) & {cluster for cluster, values in supported.items() if values}:
        domain_points = 6
    else:
        domain_points = 2

    eligibility_blocks = posting_eligibility_blocks(text) if text else []
    if eligibility_blocks:
        eligibility_points = 0
        eligibility_reason = "eligibility block: " + "; ".join(eligibility_blocks)
    elif job.get("alert_ok"):
        eligibility_points = 10
        eligibility_reason = "verified new-grad/eligible role"
    elif job.get("early_career_possible"):
        eligibility_points = 6
        eligibility_reason = "early-career possible; eligibility not verified"
    else:
        eligibility_points = 3
        eligibility_reason = "eligibility not established"

    distinctive = any(
        re.search(r"\b(won|first place|selected|grant|fellowship|led|presented to \d|\d+\+ competitors)\b", str(node.get("text") or ""), re.I)
        for node in evidence_ranked[:12]
    )
    distinctiveness_points = 5 if distinctive else (2 if ranked_nodes else 0)

    dimensions = {
        "required_coverage": required_points,
        "preferred_coverage": preferred_points,
        "evidence_strength": evidence_points,
        "domain_relevance": domain_points,
        "eligibility": eligibility_points,
        "distinctiveness": distinctiveness_points,
    }
    total = max(0, min(100, sum(dimensions.values())))
    missing = sorted(required - matched_required)
    if explicit_required:
        missing.extend("skill:%s" % token for token in sorted(required_terms - matched_terms)[:8])
    missing.extend("eligibility:%s" % block for block in eligibility_blocks)
    confidence = "high" if len(text) >= 1200 else "medium" if len(text) >= 350 else "low"
    reasons = [
        "required capabilities: %s" % (", ".join(sorted(matched_required)) or "none confirmed"),
        "preferred capabilities: %s" % (", ".join(sorted(matched_preferred)) or ("none stated" if not preferred else "none confirmed")),
        eligibility_reason,
        "posting evidence confidence: %s" % confidence,
    ]
    if direct_alignment is not None:
        reasons.insert(1, "direct title-evidence alignment: %d%%" % round(direct_alignment * 100))
    if missing:
        reasons.append("evidence gaps: " + ", ".join(missing))
    source_nodes = actionable_nodes[:6]
    source_nodes.extend(node for node in ranked_nodes if not node.get("entry_id"))
    source_nodes = source_nodes[:8]
    return {
        "version": MATCH_VERSION,
        "score": total,
        "confidence": confidence,
        "dimensions": dimensions,
        "matched_requirements": sorted(matched_required),
        "missing_requirements": missing,
        "preferred_matches": sorted(matched_preferred),
        "reasons": reasons,
        "sources": [
            {"id": node.get("id"), "source": node.get("source"), "heading": node.get("heading"), "authority": node.get("authority")}
            for node in source_nodes
        ],
        "graph_hash": graph.get("hash"),
    }


def job_match_hash(job: Dict[str, Any], posting_text: str = "") -> str:
    stable = {
        "id": job.get("id"), "company": job.get("company"), "title": job.get("title"),
        "sector": job.get("sector"), "alert_ok": job.get("alert_ok"),
        "early_career_possible": job.get("early_career_possible"), "posting_text": posting_text,
    }
    return _sha(json.dumps(stable, sort_keys=True, ensure_ascii=False), 20)


def evidence_context(
    graph: Dict[str, Any], job: Dict[str, Any], posting_text: str = "", limit: int = 120
) -> List[Dict[str, Any]]:
    """Return the most target-relevant source nodes for frontier prompts."""
    target_tokens = _tokens(
        "%s %s %s %s" % (
            job.get("company", ""), job.get("title", ""), job.get("sector", ""), posting_text[:5000]
        )
    )
    target_clusters = _cluster_hits(target_tokens)
    ranked = []
    for node in graph.get("nodes", []):
        node_tokens = set(node.get("tokens") or [])
        overlap = len(target_tokens & node_tokens)
        cluster_overlap = len(target_clusters & _cluster_hits(node_tokens))
        method_bonus = 12 if node.get("source_kind") == "methodology" else 0
        actionable_bonus = 55 if node.get("entry_id") else 0
        public_penalty = 15 if not node.get("claim_allowed") and node.get("source_kind") != "methodology" else 0
        score = (
            overlap * 12 + cluster_overlap * 20
            + int(node.get("authority") or 0) * 0.35
            + actionable_bonus + method_bonus - public_penalty
        )
        ranked.append((score, node))
    selected = [node for _, node in sorted(ranked, key=lambda item: item[0], reverse=True)[:limit]]
    return [
        {key: node.get(key) for key in ("id", "entry_id", "source", "heading", "text", "authority", "claim_allowed", "source_kind")}
        for node in selected
    ]
