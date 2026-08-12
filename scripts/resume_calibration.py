#!/usr/bin/env python3
"""Disposable local calibration lab for Resume Studio.

This deliberately stays outside the production Resume Studio UI.  It selects
varied roles from the local Job Radar snapshot, runs the existing private
tailoring engine into one ignored batch directory, and serves a tiny review
page for labeling the resulting bullets.

Run from the repository with::

    .venv/bin/python scripts/resume_calibration.py --generate --serve

Everything produced by this script lives under
``CV/.resume_studio/calibration/``.  The page emphasizes the rendered PDF; the
LaTeX source remains an artifact for debugging rather than the review surface.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import html
import json
import re
import sys
import threading
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

# Support both ``python -m scripts.resume_calibration`` and the documented
# ``python scripts/resume_calibration.py`` invocation.
SCRIPT_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(SCRIPT_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_REPO_ROOT))

from scripts import resume_studio as studio


CALIBRATION_DIRNAME = "calibration"
DEFAULT_COUNT = 8
DEFAULT_WORKERS = 1

# Ordered deliberately: specialized AI is checked before generic SWE because
# titles such as "Deep Learning Software Engineer" should remain specialized.
ROLE_FAMILIES: List[Tuple[str, Tuple[str, ...]]] = [
    (
        "data_engineering",
        (
            "data engineer",
            "data engineering",
            "analytics engineer",
            "data platform",
            "distributed data",
            "data systems",
            "data infrastructure",
        ),
    ),
    (
        "data_science",
        (
            "data scientist",
            "data science",
            "statistician",
            "biostatistician",
            "decision scientist",
            "analytics scientist",
            "quantitative analyst",
        ),
    ),
    (
        "ml_ai_engineering",
        (
            "ai engineer",
            "ml engineer",
            "machine learning engineer",
            "artificial intelligence",
            "agentic ai",
            "inference engineer",
            "research engineer",
        ),
    ),
    (
        "specialized_ai",
        (
            "computer vision",
            "audio inference",
            "model efficiency",
            "robotics software",
            "simulation fidelity",
            "deep learning",
            "vision researcher",
        ),
    ),
    (
        "software_engineering",
        (
            "software engineer",
            "backend engineer",
            "full stack",
            "application developer",
            "platform engineer",
            "api engineer",
            "systems engineer",
        ),
    ),
    (
        "cloud_devops",
        (
            "cloud engineer",
            "devops engineer",
            "site reliability",
            "infrastructure engineer",
            "platform reliability",
            "cloud infrastructure",
        ),
    ),
]


def calibration_root(root: Optional[Path] = None) -> Path:
    return studio.studio_root(root or studio.repo_root()) / CALIBRATION_DIRNAME


def batch_id() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")


def slug(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return value[:70] or "role"


def calibration_pdf_filename(job: Dict[str, Any]) -> str:
    """Name new calibration PDFs by company and calibration use case."""
    company = slug(str(job.get("company") or "company")).replace("-", "_")[:64] or "company"
    family = slug(str(job.get("calibration_role_family") or role_family(job) or "role")).replace("-", "_")[:48] or "role"
    return "%s_%s_resume.pdf" % (company, family)


def role_family(job: Dict[str, Any]) -> Optional[str]:
    title = str(job.get("title") or "").lower()
    for family, terms in ROLE_FAMILIES:
        if any(term in title for term in terms):
            return family
    return None


def _job_rank(job: Dict[str, Any]) -> Tuple[int, int, int, int]:
    return (
        int(bool(job.get("alert_ok"))),
        int(job.get("score") or 0),
        int(bool(job.get("description"))),
        int(job.get("posted_at") or 0),
    )


def select_varied_jobs(
    jobs: Iterable[Dict[str, Any]], count: int = DEFAULT_COUNT
) -> List[Dict[str, Any]]:
    """Choose at most one strong role per family before filling the batch.

    The first pass guarantees breadth.  The second pass fills a requested
    larger batch while avoiding repeated employers where possible.
    """
    candidates: List[Tuple[str, Dict[str, Any]]] = []
    for job in jobs:
        if job.get("closed_at") or not job.get("url"):
            continue
        family = role_family(job)
        if family:
            candidates.append((family, job))
    candidates.sort(key=lambda item: _job_rank(item[1]), reverse=True)
    by_family: Dict[str, List[Dict[str, Any]]] = {family: [] for family, _ in ROLE_FAMILIES}
    for family, job in candidates:
        by_family.setdefault(family, []).append(job)

    selected: List[Dict[str, Any]] = []
    selected_ids: set[str] = set()
    selected_companies: set[str] = set()
    selected_families: set[str] = set()

    def add(family: str, job: Dict[str, Any]) -> None:
        job_id = str(job.get("id") or "")
        if not job_id or job_id in selected_ids:
            return
        value = dict(job)
        value["calibration_role_family"] = family
        selected.append(value)
        selected_ids.add(job_id)
        selected_companies.add(str(job.get("company") or "").lower())
        selected_families.add(family)

    # Walk families in a stable order, taking the strongest employer that has
    # not already appeared.  This prevents a company with many openings from
    # swallowing the calibration set.
    for family, _ in ROLE_FAMILIES:
        if len(selected) >= max(1, count):
            break
        for job in by_family.get(family, []):
            company = str(job.get("company") or "").lower()
            if company and company in selected_companies:
                continue
            add(family, job)
            break

    for family, job in candidates:
        if len(selected) >= max(1, count):
            break
        company = str(job.get("company") or "").lower()
        if str(job.get("id") or "") in selected_ids:
            continue
        if company and company in selected_companies:
            continue
        add(family, job)

    for family, job in candidates:
        if len(selected) >= max(1, count):
            break
        add(family, job)
    return selected


def _bullet_rows(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    plan = report.get("content_plan") or {}
    for section in ("experiences", "projects", "leadership"):
        for entry in plan.get(section) or []:
            for index, bullet in enumerate(entry.get("bullets") or [], 1):
                rows.append(
                    {
                        "id": str(bullet.get("source_id") or "%s-%s" % (section, index)),
                        "section": section,
                        "entry": str(entry.get("source_id") or ""),
                        "text": str(bullet.get("text") or ""),
                        "priority": bullet.get("priority"),
                        "evidence_ids": bullet.get("evidence_ids") or [],
                        "candidate_rationale": bullet.get("candidate_rationale") or "",
                    }
                )
    return rows


def _case_record(
    batch_dir: Path,
    run_dir: Path,
    job: Dict[str, Any],
    family: str,
    error: str = "",
) -> Dict[str, Any]:
    report = studio.read_json(run_dir / "report.json", {}) or {}
    pdf_name = str(studio.run_pdf_path(run_dir).name)
    preview_name = str(studio.run_preview_path(run_dir).name)
    has_pdf = (run_dir / pdf_name).exists()
    status = "failed" if error else "complete" if report else "partial" if has_pdf else "running"
    return {
        "case_id": run_dir.name,
        "batch_id": batch_dir.name,
        "status": status,
        "error": error,
        "role_family": family,
        "job": studio.job_summary(job),
        "pdf_filename": pdf_name,
        "preview_filename": preview_name,
        "run_dir": str(run_dir.relative_to(batch_dir.parent.parent)),
        "artifacts": {
            "pdf": (run_dir / pdf_name).exists(),
            "preview": (run_dir / preview_name).exists(),
            "report": (run_dir / "report.json").exists(),
        },
        "thesis": report.get("positioning_thesis", ""),
        "craft_score": (report.get("review") or {}).get("craft_score"),
        "ready": (report.get("review") or {}).get("ready"),
        "layout": ((report.get("review") or {}).get("deterministic") or {}).get("layout", {}),
        "bullets": _bullet_rows(report),
    }


def _run_one(
    batch_dir: Path,
    job: Dict[str, Any],
    mode: str,
) -> Dict[str, Any]:
    family = str(job.get("calibration_role_family") or role_family(job) or "other")
    case_id = "%s-%s-%s" % (
        slug(family),
        slug(str(job.get("company") or "company")),
        str(job.get("id") or uuid.uuid4().hex[:8])[:18],
    )
    run_dir = batch_dir / "runs" / case_id
    run_dir.mkdir(parents=True, exist_ok=True)
    pdf_filename = calibration_pdf_filename(job)
    studio.write_json(
        run_dir / "status.json",
        {"pdf_filename": pdf_filename, "preview_filename": Path(pdf_filename).stem + "-preview.png"},
    )
    studio.write_json(
        run_dir / "calibration_case.json",
        {"case_id": case_id, "batch_id": batch_dir.name, "role_family": family, "job": studio.job_summary(job)},
    )

    def update(step: str, message: str, **extra: Any) -> None:
        studio.write_json(
            run_dir / "calibration_status.json",
            {"step": step, "message": message, "updated_at": studio.now_iso(), **extra},
        )

    try:
        # ``dream`` is the current reviewable enhancement path.  ``strict``
        # remains available so the same lab can compare selection vs wording.
        studio.run_tailoring(run_dir, job, update, enhance=(mode == "dream"))
        return _case_record(batch_dir, run_dir, job, family)
    except Exception as exc:  # retain partial artifacts for diagnosis
        (run_dir / "calibration_error.txt").write_text(str(exc) + "\n")
        return _case_record(batch_dir, run_dir, job, family, str(exc))


def generate_batch(
    root: Optional[Path] = None,
    count: int = DEFAULT_COUNT,
    mode: str = "dream",
    workers: int = DEFAULT_WORKERS,
) -> Tuple[Path, List[Dict[str, Any]]]:
    if mode not in {"dream", "strict"}:
        raise ValueError("mode must be dream or strict")
    base = root or studio.repo_root()
    jobs = studio.current_scored_jobs(base)
    selected = select_varied_jobs(jobs.values(), count=count)
    if not selected:
        raise RuntimeError("No varied roles were found in state/jobs.json")
    batch_dir = calibration_root(base) / batch_id()
    batch_dir.mkdir(parents=True, exist_ok=True)
    studio.write_json(
        batch_dir / "selection.json",
        {
            "created_at": studio.now_iso(),
            "mode": mode,
            "requested_count": count,
            "jobs": [studio.job_summary(job) | {"role_family": job.get("calibration_role_family")} for job in selected],
        },
    )
    results: List[Dict[str, Any]] = []
    max_workers = max(1, min(int(workers), len(selected)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_run_one, batch_dir, job, mode) for job in selected]
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
            results.sort(key=lambda item: item.get("role_family", ""))
            studio.write_json(batch_dir / "cases.json", results)
    studio.write_json(
        batch_dir / "index.json",
        {"batch_id": batch_dir.name, "mode": mode, "created_at": studio.now_iso(), "cases": results},
    )
    return batch_dir, results


def latest_batch(root: Optional[Path] = None) -> Optional[Path]:
    base = calibration_root(root)
    batches = [path for path in base.iterdir() if path.is_dir()] if base.exists() else []
    batches = [path for path in batches if (path / "index.json").exists()]
    return sorted(batches)[-1] if batches else None


def _batch_cases(batch_dir: Path) -> List[Dict[str, Any]]:
    """Read a finalized batch, or recover cases from an interrupted batch."""
    indexed = studio.read_json(batch_dir / "index.json", {}) or {}
    if isinstance(indexed.get("cases"), list):
        return indexed["cases"]
    recovered: List[Dict[str, Any]] = []
    runs_dir = batch_dir / "runs"
    if not runs_dir.exists():
        return recovered
    for run_dir in sorted(path for path in runs_dir.iterdir() if path.is_dir()):
        meta = studio.read_json(run_dir / "calibration_case.json", {}) or {}
        job = meta.get("job") if isinstance(meta.get("job"), dict) else {}
        if not job:
            continue
        error_path = run_dir / "calibration_error.txt"
        error = error_path.read_text(errors="replace").strip() if error_path.exists() else ""
        recovered.append(
            _case_record(
                batch_dir,
                run_dir,
                job,
                str(meta.get("role_family") or "other"),
                error,
            )
        )
    return recovered


def all_cases(root: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Aggregate the useful cases across calibration attempts.

    A provider outage can leave an interrupted batch without ``index.json``;
    completed run directories are still valid review material.  Repeated job
    IDs are deduplicated in favor of the newest complete case.
    """
    base = calibration_root(root)
    if not base.exists():
        return []
    by_case: Dict[str, Dict[str, Any]] = {}
    for batch_dir in sorted(path for path in base.iterdir() if path.is_dir()):
        for case in _batch_cases(batch_dir):
            case_id = str(case.get("case_id") or "")
            if not case_id:
                continue
            old = by_case.get(case_id)
            if old is None:
                by_case[case_id] = case
                continue
            new_usable = bool(case.get("artifacts", {}).get("pdf"))
            old_usable = bool(old.get("artifacts", {}).get("pdf"))
            new_complete = case.get("status") == "complete"
            old_complete = old.get("status") == "complete"
            if (
                (new_usable and not old_usable)
                or (new_usable == old_usable and new_complete and not old_complete)
                or (
                    new_usable == old_usable
                    and new_complete == old_complete
                    and str(case.get("batch_id")) > str(old.get("batch_id"))
                )
            ):
                by_case[case_id] = case
    return sorted(by_case.values(), key=lambda item: (str(item.get("role_family")), str(item.get("case_id"))))


def _load_cases(root: Path, batch_id_value: str = "") -> Tuple[Optional[Path], List[Dict[str, Any]]]:
    base = calibration_root(root)
    if not batch_id_value:
        return None, all_cases(root)
    target = base / batch_id_value
    if not target.is_dir():
        return None, []
    return target, _batch_cases(target)


def _artifact_path(root: Path, case: Dict[str, Any], name: str) -> Optional[Path]:
    pdf_name = str(case.get("pdf_filename") or "company_calibration_resume.pdf")
    preview_name = str(case.get("preview_filename") or Path(pdf_name).stem + "-preview.png")
    allowed = {pdf_name: pdf_name, preview_name: preview_name, "report.json": "report.json", "job_context.json": "job_context.json"}
    relative = allowed.get(name)
    if not relative:
        return None
    run_dir = (calibration_root(root).parent / str(case.get("run_dir") or "")).resolve()
    target = (run_dir / relative).resolve()
    if calibration_root(root).resolve() not in target.parents or not target.is_file():
        return None
    return target


def save_feedback(root: Path, payload: Dict[str, Any]) -> Dict[str, Any]:
    case_id = str(payload.get("case_id") or "")
    batch, cases = _load_cases(root, str(payload.get("batch_id") or ""))
    if not batch or not any(str(case.get("case_id")) == case_id for case in cases):
        raise ValueError("unknown calibration case")
    value = {
        "saved_at": studio.now_iso(),
        "case_id": case_id,
        "batch_id": batch.name,
        "overall": str(payload.get("overall") or ""),
        "reasons": [str(item) for item in payload.get("reasons") or []][:20],
        "notes": str(payload.get("notes") or "")[:4000],
        "bullets": payload.get("bullets") if isinstance(payload.get("bullets"), list) else [],
    }
    with (batch / "feedback.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")
    return value


def _js(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


LAB_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Resume calibration lab</title>
<style>
:root{color-scheme:dark;--bg:#0e1117;--panel:#161b22;--line:#30363d;--muted:#8b949e;--text:#f0f6fc;--blue:#58a6ff;--green:#3fb950;--yellow:#d29922;--red:#f85149}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}main{max-width:1450px;margin:0 auto;padding:24px 18px 70px}h1{margin:0 0 4px;font-size:27px}h2{font-size:18px;margin:0 0 12px}.muted{color:var(--muted)}.grid{display:grid;grid-template-columns:340px minmax(0,1fr);gap:16px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:14px}.cases{max-height:calc(100vh - 170px);overflow:auto}.case{display:block;width:100%;text-align:left;background:transparent;color:var(--text);border:1px solid transparent;border-radius:7px;padding:10px;margin:4px 0;cursor:pointer}.case:hover,.case.selected{background:#1f2937;border-color:var(--blue)}.case strong,.case small{display:block}.case small{color:var(--muted);margin-top:3px}.badge{display:inline-block;border:1px solid var(--line);border-radius:99px;padding:2px 7px;margin:2px 4px 2px 0;font-size:12px}.score{font-size:23px;margin:0 0 5px}.toolbar{display:flex;gap:8px;align-items:center;margin-bottom:12px}select,textarea,button{font:inherit}select,textarea{background:#0d1117;border:1px solid var(--line);border-radius:6px;color:var(--text);padding:8px}textarea{width:100%;min-height:92px;resize:vertical}button{background:#238636;border:1px solid #2ea043;color:white;border-radius:6px;padding:8px 11px;cursor:pointer}button.secondary{background:#21262d;border-color:var(--line)}button:disabled{opacity:.5;cursor:wait}.case-grid{display:grid;grid-template-columns:minmax(420px,1.1fr) minmax(360px,.9fr);gap:16px}.pdf{width:100%;height:780px;border:1px solid var(--line);border-radius:6px;background:white}.meta{font-size:13px;color:var(--muted)}.thesis{border-left:3px solid var(--blue);padding:8px 10px;background:#111827;margin:10px 0}.bullet{border:1px solid var(--line);border-radius:7px;padding:10px;margin:8px 0;background:#0d1117}.bullet-text{margin:0 0 7px}.bullet-meta{font-size:12px;color:var(--muted)}.feedback{margin-top:14px;border-top:1px solid var(--line);padding-top:14px}.feedback-grid{display:grid;grid-template-columns:1fr 1fr;gap:7px}.checks label{display:inline-block;margin:4px 10px 4px 0;color:var(--muted);font-size:13px}.status{margin-top:8px;color:var(--green)}.empty{padding:30px;color:var(--muted)}a{color:var(--blue)}@media(max-width:1050px){.case-grid{grid-template-columns:1fr}.pdf{height:680px}}@media(max-width:760px){.grid{grid-template-columns:1fr}.cases{max-height:350px}.feedback-grid{grid-template-columns:1fr}}
</style></head><body><main>
<h1>Resume calibration lab</h1><p class="muted">Varied radar postings · review the rendered PDF · label the bullets · output stays in CV/.resume_studio/calibration/</p>
<div class="grid"><section class="panel"><div class="toolbar"><select id="family"><option value="">All role families</option></select><button class="secondary" id="reload">Reload</button></div><div id="cases" class="cases">Loading…</div></section>
<section class="panel"><div id="empty" class="empty">Select a generated case.</div><div id="detail" hidden></div></section></div>
<script>
let state={batch:null,cases:[],selected:null};
const $=id=>document.getElementById(id);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function load(){const r=await fetch('/api/cases');state=await r.json();const families=[...new Set(state.cases.map(c=>c.role_family))].sort();$('family').innerHTML='<option value="">All role families</option>'+families.map(x=>`<option>${esc(x)}</option>`).join('');renderCases();}
function renderCases(){const f=$('family').value;const rows=state.cases.filter(c=>!f||c.role_family===f);$('cases').innerHTML=rows.map(c=>{const j=c.job||{};return `<button class="case ${state.selected===c.case_id?'selected':''}" data-id="${esc(c.case_id)}"><strong>${esc(j.company)} · ${esc(j.title)}</strong><small>${esc(c.role_family)} · ${c.craft_score??'—'}/100 · ${esc(c.status)}</small></button>`}).join('')||'<p class="muted">No cases.</p>';document.querySelectorAll('.case').forEach(b=>b.onclick=()=>choose(b.dataset.id));}
async function choose(id){state.selected=id;renderCases();const r=await fetch('/api/case?id='+encodeURIComponent(id));const c=await r.json();const j=c.job||{};let h=`<div class="case-grid"><div><h2>${esc(j.company)} · ${esc(j.title)}</h2><div class="meta">${esc(c.role_family)} · Radar ${j.score??'—'} · <a href="${esc(j.url||'#')}" target="_blank" rel="noreferrer">open posting</a></div><div class="thesis"><strong>Positioning thesis</strong><br>${esc(c.thesis||'No thesis returned')}</div><iframe class="pdf" src="/api/artifact?case=${encodeURIComponent(c.case_id)}&name=resume.pdf" title="Rendered resume PDF"></iframe><p class="meta"><a href="/api/artifact?case=${encodeURIComponent(c.case_id)}&name=resume.pdf" target="_blank">Open PDF in a tab</a> · ${c.artifacts?.preview?`<a href="/api/artifact?case=${encodeURIComponent(c.case_id)}&name=resume-preview.png" target="_blank">Open PNG preview</a>`:''}</p></div><div><div class="score">${c.craft_score??'—'}/100 craft</div><div>${c.ready?'':'Needs revision or fact verification'}</div><p class="meta">The PDF is the review surface. Source LaTeX is intentionally not shown here.</p><h2>Bullet labels</h2>${(c.bullets||[]).map((b,i)=>`<div class="bullet"><p class="bullet-text">${esc(b.text)}</p><div class="bullet-meta">${esc(b.section)} · priority ${esc(b.priority??'—')} · evidence ${(b.evidence_ids||[]).length}</div><select data-bullet="${i}"><option value="">Unlabeled</option><option value="good">Good</option><option value="revise">Revise</option><option value="bad">Bad</option><option value="needs_information">Needs information</option></select></div>`).join('')||'<p class="muted">No bullets were returned.</p>'}<div class="feedback"><h2>Overall feedback</h2><div class="feedback-grid"><select id="overall"><option value="">Choose label…</option><option value="good">Good</option><option value="revise">Revise</option><option value="bad">Bad</option><option value="needs_information">Needs information</option></select><button id="save">Save feedback</button></div><div class="checks"><label><input type="checkbox" value="too_short"> too short</label><label><input type="checkbox" value="generic"> generic</label><label><input type="checkbox" value="weak_xyz"> weak XYZ</label><label><input type="checkbox" value="wrong_role"> wrong role</label><label><input type="checkbox" value="unsupported"> unsupported</label><label><input type="checkbox" value="redundant"> redundant</label><label><input type="checkbox" value="good_specificity"> good specificity</label></div><textarea id="notes" placeholder="Why is this good or bad? What should change?"></textarea><div id="save-status" class="status"></div></div></div></div>`;$('empty').hidden=true;$('detail').hidden=false;$('detail').innerHTML=h;$('save').onclick=()=>saveFeedback(c);}
async function saveFeedback(c){const bullets=[...document.querySelectorAll('[data-bullet]')].map((el,i)=>({id:c.bullets[i]?.id||'',label:el.value}));const reasons=[...document.querySelectorAll('.checks input:checked')].map(x=>x.value);const body={batch_id:c.batch_id,case_id:c.case_id,overall:$('overall').value,reasons,notes:$('notes').value,bullets};const r=await fetch('/api/feedback',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});$('save-status').textContent=r.ok?'Saved to this calibration batch.':(await r.json()).error||'Could not save';}
$('family').onchange=renderCases;$('reload').onclick=load;load();
</script></main></body></html>'''

LAB_HTML = LAB_HTML.replace(
    "name=resume.pdf",
    "name=${encodeURIComponent(c.pdf_filename || 'company_calibration_resume.pdf')}",
).replace(
    "name=resume-preview.png",
    "name=${encodeURIComponent(c.preview_filename || 'company_calibration_resume-preview.png')}",
)


class CalibrationHandler(BaseHTTPRequestHandler):
    root: Path = studio.repo_root()

    def log_message(self, fmt: str, *args: Any) -> None:
        print("resume-calibration:", fmt % args)

    def _json(self, value: Any, status: int = HTTPStatus.OK) -> None:
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _bytes(self, value: bytes, content_type: str, status: int = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(value)))
        self.end_headers()
        self.wfile.write(value)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            return self._bytes(LAB_HTML.encode("utf-8"), "text/html; charset=utf-8")
        if parsed.path == "/api/cases":
            batch, cases = _load_cases(self.root)
            usable = [
                case for case in cases
                if case.get("status") in {"complete", "partial"}
                and (case.get("artifacts") or {}).get("pdf")
            ]
            return self._json({"batch_id": batch.name if batch else "all", "cases": usable})
        if parsed.path == "/api/case":
            case_id = parse_qs(parsed.query).get("id", [""])[0]
            batch, cases = _load_cases(self.root)
            case = next((item for item in cases if item.get("case_id") == case_id), None)
            if not case:
                return self._json({"error": "case not found"}, HTTPStatus.NOT_FOUND)
            return self._json(case)
        if parsed.path == "/api/artifact":
            params = parse_qs(parsed.query)
            case_id = params.get("case", [""])[0]
            name = params.get("name", [""])[0]
            _, cases = _load_cases(self.root)
            case = next((item for item in cases if item.get("case_id") == case_id), None)
            target = _artifact_path(self.root, case or {}, name) if case else None
            if not target:
                return self._json({"error": "artifact not found"}, HTTPStatus.NOT_FOUND)
            pdf_name = str(case.get("pdf_filename") or "company_calibration_resume.pdf")
            preview_name = str(case.get("preview_filename") or Path(pdf_name).stem + "-preview.png")
            content_type = (
                "application/pdf" if name == pdf_name else
                "image/png" if name == preview_name else
                "application/json"
            )
            return self._bytes(target.read_bytes(), content_type)
        return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/feedback":
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 100_000:
                raise ValueError("request too large")
            payload = json.loads(self.rfile.read(length) or b"{}")
            saved = save_feedback(self.root, payload)
            return self._json({"ok": True, "feedback": saved})
        except (ValueError, json.JSONDecodeError) as exc:
            return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)


def serve(root: Path, host: str = "127.0.0.1", port: int = 4321) -> int:
    CalibrationHandler.root = root
    server = ThreadingHTTPServer((host, port), CalibrationHandler)
    print("Resume calibration lab: http://%s:%s/" % (host, port))
    print("Calibration output: %s" % calibration_root(root))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nResume calibration lab stopped")
    finally:
        server.server_close()
    return 0


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Generate and review varied private resume tailoring cases")
    parser.add_argument("--generate", action="store_true", help="generate a new varied batch from state/jobs.json")
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    parser.add_argument("--mode", choices=("dream", "strict"), default="dream")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--serve", action="store_true", help="serve the latest batch review page")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4321)
    args = parser.parse_args(list(argv) if argv is not None else None)
    root = studio.repo_root()
    if args.generate:
        batch, cases = generate_batch(root, count=args.count, mode=args.mode, workers=args.workers)
        print("Generated %s cases in %s" % (len(cases), batch))
        for case in cases:
            print("- %s: %s · %s" % (case.get("role_family"), case.get("job", {}).get("company"), case.get("status")))
    if args.serve or not args.generate:
        return serve(root, args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
