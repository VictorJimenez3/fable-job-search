"""Private, local-only project workspace for Resume Studio.

The project service is deliberately independent from the tailoring engine.  It
maps the existing CV tree into read-only logical projects and stores new
editable projects below ``CV/.resume_studio/projects`` without moving or
rewriting any legacy artifact.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


TEXT_EXTENSIONS = {".tex", ".bib", ".sty", ".cls", ".md", ".txt"}
ASSET_EXTENSIONS = {".png", ".jpg", ".jpeg", ".pdf"}
ALLOWED_EXTENSIONS = TEXT_EXTENSIONS | ASSET_EXTENSIONS
MAX_TEXT_BYTES = 512 * 1024
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_PROJECT_BYTES = 50 * 1024 * 1024
MAX_PROJECT_FILES = 200
COMPILE_TIMEOUT_SECONDS = 60
MAX_LOG_BYTES = 2 * 1024 * 1024
PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")


class ProjectError(ValueError):
    """A safe, user-facing project operation error."""


class ProjectConflict(ProjectError):
    """Optimistic-concurrency conflict."""

    status_code = 409


class ProjectForbidden(ProjectError):
    """An operation is not allowed for a read-only logical project."""

    status_code = 403


class ProjectNotFound(ProjectError):
    status_code = 404


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_path(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def _sanitize_log(value: str) -> str:
    """Keep diagnostics useful without returning local absolute paths."""
    text = str(value or "")
    text = re.sub(r"(?:/Users|/private|/var|/tmp|/opt)/[^\s'\"]+", "<local-path>", text)
    return text[-MAX_LOG_BYTES:]


def _json_dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    tmp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    tmp.write_bytes(raw)
    os.replace(tmp, path)


def _json_load(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def workspace_root(cv_root: Path) -> Path:
    return cv_root / ".resume_studio"


def projects_root(cv_root: Path) -> Path:
    return workspace_root(cv_root) / "projects"


def ensure_workspace(cv_root: Path) -> Path:
    root = projects_root(cv_root)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_path(value: str, *, allow_empty: bool = False) -> str:
    raw = str(value or "").replace("\\", "/")
    if not raw and allow_empty:
        return ""
    if not raw or raw.startswith("/") or "\x00" in raw:
        raise ProjectError("path is required and must be project-relative")
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ProjectError("path traversal is not allowed")
    if len(parts) > 12 or any(len(part) > 128 for part in parts):
        raise ProjectError("path is too deep or long")
    return "/".join(parts)


def _project_id(value: str) -> str:
    value = str(value or "")
    if not PROJECT_ID_RE.fullmatch(value):
        raise ProjectError("invalid project id")
    return value


def _name_to_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")[:48] or "project"
    return slug + "-" + uuid.uuid4().hex[:8]


def _extension(path: str) -> str:
    return Path(path).suffix.lower()


def validate_file_path(path: str, *, folder: bool = False) -> str:
    path = _safe_path(path)
    if path.startswith("history/") or path.startswith("generated/") or path.startswith(".trash/"):
        raise ProjectError("managed history and generated artifacts are not directly editable")
    if not (path.startswith("source/") or path.startswith("assets/")):
        raise ProjectError("files must live under source/ or assets/")
    if not folder:
        ext = _extension(path)
        if ext not in ALLOWED_EXTENSIONS:
            raise ProjectError("unsupported file extension")
    return path


def _is_symlink_free(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve()
        return root.resolve() == resolved or root.resolve() in resolved.parents and not any(p.is_symlink() for p in path.parents if p.exists()) and not path.is_symlink()
    except OSError:
        return False


def _manifest(project_dir: Path) -> Dict[str, Any]:
    value = _json_load(project_dir / "project.json")
    if not isinstance(value, dict):
        raise ProjectNotFound("project not found")
    value.setdefault("id", project_dir.name)
    value.setdefault("kind", "managed")
    return value


def _managed_dir(cv_root: Path, project_id: str) -> Path:
    project_id = _project_id(project_id)
    path = ensure_workspace(cv_root) / project_id
    if path.is_symlink() or not path.is_dir() or not (path / "project.json").is_file():
        raise ProjectNotFound("project not found")
    return path


def _legacy_project_specs(cv_root: Path) -> List[Dict[str, Any]]:
    def spec(pid: str, name: str, description: str, files: Iterable[Tuple[str, str]], kind: str = "protected") -> Dict[str, Any]:
        return {"id": pid, "name": name, "kind": kind, "editable": False, "archived": False, "description": description, "files": [{"path": p, "legacy_path": lp} for p, lp in files]}

    specs = [spec("canonical-resume", "Canonical resume", "Locked visual template and generated PDF.", [("source/VictorJimenezResume.tex", "immutable/VictorJimenezResume.tex"), ("generated/VictorJimenezResume.pdf", "immutable/VictorJimenezResume.pdf")]),
             spec("historical-resume", "Historical resume", "Historical protected reference.", [("source/og_resume.tex", "immutable/og_resume.tex"), ("generated/og_resume.pdf", "immutable/og_resume.pdf")]),
             spec("tldp-resume", "TLDP resume", "TLDP protected reference.", [("source/tldp_resume.tex", "immutable/tldp_resume.tex"), ("generated/tldp_resume.pdf", "immutable/tldp_resume.pdf")]),
             spec("master-cv", "Master CV", "Read-only source; clone it to edit privately.", [("source/cv_full.tex", "cv_full.tex")], kind="master")]
    runs = workspace_root(cv_root) / "runs"
    grouped: Dict[str, List[Path]] = {}
    if runs.is_dir():
        for run in sorted(runs.iterdir()):
            if not run.is_dir() or not re.fullmatch(r"[a-f0-9]{12}", run.name):
                continue
            report = _json_load(run / "report.json", {}) or {}
            status = _json_load(run / "status.json", {}) or {}
            job = (status.get("job") if isinstance(status, dict) else {}) or (report.get("job") if isinstance(report, dict) else {})
            key = str((job or {}).get("id") or (job or {}).get("company") or "run")
            grouped.setdefault(key, []).append(run)
    for key, run_dirs in grouped.items():
        latest = run_dirs[-1]
        rid = re.sub(r"[^a-z0-9_-]+", "-", key.lower()).strip("-")[:44] or "posting"
        pid = "tailored-" + rid
        if not PROJECT_ID_RE.fullmatch(pid):
            pid = "tailored-" + uuid.uuid4().hex[:8]
        report = _json_load(latest / "report.json", {}) or {}
        status = _json_load(latest / "status.json", {}) or {}
        job = (status.get("job") if isinstance(status, dict) else {}) or (report.get("job") if isinstance(report, dict) else {})
        files = [("source/resume.tex", "runs/%s/resume.tex" % latest.name)]
        for filename in ("victor_jimenez_company.pdf", "resume.pdf", "preview.png", "report.json", "content_plan.json"):
            if (latest / filename).is_file():
                target = "generated/%s" % filename if filename.endswith((".pdf", ".png")) else "history/%s" % filename
                files.append((target, "runs/%s/%s" % (latest.name, filename)))
        specs.append({"id": pid, "name": "Tailored · %s" % ((job or {}).get("company") or key), "kind": "tailored-run", "editable": False, "archived": False, "description": "Read-only tailored run; edit through Workshop.", "files": [{"path": p, "legacy_path": lp} for p, lp in files], "run_ids": [run.name for run in run_dirs]})
    return specs


def _all_specs(cv_root: Path) -> List[Dict[str, Any]]:
    specs = _legacy_project_specs(cv_root)
    root = ensure_workspace(cv_root)
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or directory.name.startswith("."):
            continue
        try:
            manifest = _manifest(directory)
        except ProjectNotFound:
            continue
        manifest["editable"] = not bool(manifest.get("archived_at"))
        manifest["archived"] = bool(manifest.get("archived_at"))
        managed_files = []
        try:
            for path in _iter_managed_files(directory):
                rel = path.relative_to(directory).as_posix()
                managed_files.append({"path": rel, "size": path.stat().st_size, "sha256": sha256_path(path), "kind": "source" if rel.startswith("source/") else "asset", "read_only": bool(manifest.get("archived_at"))})
        except ProjectError:
            managed_files = []
        manifest["files"] = sorted(managed_files, key=lambda x: x["path"])
        specs.append(manifest)
    return specs


def list_projects(cv_root: Path) -> Dict[str, Any]:
    return {"projects": _all_specs(cv_root), "limits": {"max_text_bytes": MAX_TEXT_BYTES, "max_file_bytes": MAX_FILE_BYTES, "max_project_bytes": MAX_PROJECT_BYTES, "max_project_files": MAX_PROJECT_FILES}}


def _find_spec(cv_root: Path, project_id: str) -> Dict[str, Any]:
    for item in _all_specs(cv_root):
        if item.get("id") == project_id:
            return item
    raise ProjectNotFound("project not found")


def _resolve_legacy(cv_root: Path, spec: Dict[str, Any], rel: str) -> Optional[Path]:
    for item in spec.get("files", []):
        if item.get("path") == rel:
            candidate = (cv_root / str(item.get("legacy_path") or "")).resolve()
            cv_resolved = cv_root.resolve()
            if cv_resolved not in candidate.parents or not candidate.is_file():
                return None
            return candidate
    return None


def _project_file(project_dir: Path, rel: str) -> Path:
    rel = validate_file_path(rel)
    target = (project_dir / rel).resolve()
    root = project_dir.resolve()
    if root not in target.parents or not _is_symlink_free(target, root):
        raise ProjectError("symlink or traversal is not allowed")
    return target


def _project_any_path(project_dir: Path, rel: str) -> Path:
    rel = _safe_path(rel)
    if not (rel.startswith("source/") or rel.startswith("assets/")):
        raise ProjectError("files must live under source/ or assets/")
    target = (project_dir / rel).resolve()
    root = project_dir.resolve()
    if root not in target.parents or not _is_symlink_free(target, root):
        raise ProjectError("symlink or traversal is not allowed")
    return target


def _iter_managed_files(project_dir: Path) -> List[Path]:
    result: List[Path] = []
    for base in (project_dir / "source", project_dir / "assets"):
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.is_file() and not path.is_symlink():
                result.append(path)
            elif path.is_symlink():
                raise ProjectError("symlinks are not allowed in projects")
    return result


def _usage(project_dir: Path) -> Tuple[int, int]:
    files = _iter_managed_files(project_dir)
    return len(files), sum(path.stat().st_size for path in files)


def list_project_files(cv_root: Path, project_id: str, *, include_history: bool = True) -> Dict[str, Any]:
    spec = _find_spec(cv_root, project_id)
    files: List[Dict[str, Any]] = []
    if spec.get("kind") != "managed":
        for item in spec.get("files", []):
            path = _resolve_legacy(cv_root, spec, str(item.get("path") or ""))
            if path:
                files.append({"path": item["path"], "size": path.stat().st_size, "sha256": sha256_path(path), "kind": "source" if item["path"].startswith("source/") else "generated", "read_only": True})
        return {"project": {k: spec.get(k) for k in ("id", "name", "kind", "editable", "archived", "description")}, "files": files}
    directory = _managed_dir(cv_root, project_id)
    for path in _iter_managed_files(directory):
        rel = path.relative_to(directory).as_posix()
        files.append({"path": rel, "size": path.stat().st_size, "sha256": sha256_path(path), "kind": "source" if rel.startswith("source/") else "asset", "read_only": bool(spec.get("archived"))})
    if include_history:
        history = directory / "history"
        if history.is_dir():
            for revision in sorted(history.iterdir()):
                manifest = _json_load(revision / "revision.json", {}) or {}
                files.append({"path": "history/%s" % revision.name, "kind": "revision", "created_at": manifest.get("created_at"), "label": manifest.get("label", "Save")})
    return {"project": {k: spec.get(k) for k in ("id", "name", "kind", "editable", "archived", "description", "main_file")}, "files": sorted(files, key=lambda x: x.get("path", ""))}


def read_file(cv_root: Path, project_id: str, rel: str) -> Dict[str, Any]:
    rel = _safe_path(rel)
    spec = _find_spec(cv_root, project_id)
    if spec.get("kind") != "managed":
        path = _resolve_legacy(cv_root, spec, rel)
        if path is None:
            raise ProjectNotFound("file not found")
    else:
        path = _project_file(_managed_dir(cv_root, project_id), rel)
    if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise ProjectNotFound("file not found")
    raw = path.read_bytes()
    result = {"project_id": project_id, "path": rel, "sha256": sha256_bytes(raw), "size": len(raw), "extension": path.suffix.lower(), "read_only": spec.get("kind") != "managed" or bool(spec.get("archived"))}
    if path.suffix.lower() in TEXT_EXTENSIONS:
        if len(raw) > MAX_TEXT_BYTES:
            raise ProjectError("text file exceeds 512 KiB")
        try:
            result["content"] = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProjectError("text sources must be UTF-8") from exc
        result["encoding"] = "utf-8"
    else:
        result["binary"] = True
    return result


def artifact(cv_root: Path, project_id: str, rel: str) -> Tuple[Path, str]:
    rel = _safe_path(rel)
    spec = _find_spec(cv_root, project_id)
    if spec.get("kind") != "managed":
        path = _resolve_legacy(cv_root, spec, rel)
    else:
        path = ( _managed_dir(cv_root, project_id) / rel).resolve()
        root = _managed_dir(cv_root, project_id).resolve()
        if root not in path.parents or path.is_symlink():
            path = None
    if path is None or not path.is_file():
        raise ProjectNotFound("artifact not found")
    return path, mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _snapshot(project_dir: Path, label: str) -> Dict[str, Any]:
    revision_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
    revision = project_dir / "history" / revision_id
    snapshot_dir = revision / "snapshot"
    snapshot_dir.mkdir(parents=True, exist_ok=False)
    entries = []
    for path in _iter_managed_files(project_dir):
        rel = path.relative_to(project_dir).as_posix()
        target = snapshot_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        entries.append({"path": rel, "sha256": sha256_path(path), "size": path.stat().st_size})
    meta = {"revision_id": revision_id, "created_at": now_iso(), "label": str(label or "Save"), "files": entries, "manifest": _json_load(project_dir / "project.json", {}) or {}}
    _json_dump(revision / "revision.json", meta)
    return meta


def _check_limits(project_dir: Path) -> None:
    count, total = _usage(project_dir)
    if count > MAX_PROJECT_FILES:
        raise ProjectError("project exceeds 200 files")
    if total > MAX_PROJECT_BYTES:
        raise ProjectError("project exceeds 50 MiB")
    for path in _iter_managed_files(project_dir):
        limit = MAX_TEXT_BYTES if path.suffix.lower() in TEXT_EXTENSIONS else MAX_FILE_BYTES
        if path.stat().st_size > limit:
            raise ProjectError("file exceeds its size limit")


def _check_write_limits(project_dir: Path, target: Path, raw: bytes) -> None:
    """Validate a prospective write before changing the project."""
    count, total = _usage(project_dir)
    old_size = target.stat().st_size if target.is_file() else 0
    new_count = count if target.is_file() else count + 1
    if new_count > MAX_PROJECT_FILES:
        raise ProjectError("project exceeds 200 files")
    if len(raw) > (MAX_TEXT_BYTES if target.suffix.lower() in TEXT_EXTENSIONS else MAX_FILE_BYTES):
        raise ProjectError("file exceeds its size limit")
    if total - old_size + len(raw) > MAX_PROJECT_BYTES:
        raise ProjectError("project exceeds 50 MiB")


def create_project(cv_root: Path, name: str, *, template: str = "blank") -> Dict[str, Any]:
    name = str(name or "").strip()[:120]
    if not name:
        raise ProjectError("project name is required")
    root = ensure_workspace(cv_root)
    pid = _name_to_id(name)
    directory = root / pid
    directory.mkdir(parents=True, exist_ok=False)
    for folder in ("source", "assets", "generated", "history"):
        (directory / folder).mkdir()
    manifest = {"version": 1, "id": pid, "name": name, "kind": "managed", "description": "Private Resume Studio project", "main_file": "source/main.tex", "created_at": now_iso(), "updated_at": now_iso(), "archived_at": "", "origin": template if template != "blank" else ""}
    _json_dump(directory / "project.json", manifest)
    if template != "blank":
        source = None
        if template in {"master-cv", "canonical-resume", "historical-resume", "tldp-resume"}:
            spec = _find_spec(cv_root, template)
            candidates = [item for item in spec.get("files", []) if str(item.get("path", "")).startswith("source/")]
            if candidates:
                source = _resolve_legacy(cv_root, spec, candidates[0]["path"])
        elif template.startswith("tailored-"):
            spec = _find_spec(cv_root, template)
            source = _resolve_legacy(cv_root, spec, "source/resume.tex")
        if source and source.is_file():
            target = directory / "source" / ("main.tex" if source.suffix.lower() == ".tex" else source.name)
            shutil.copy2(source, target)
            manifest["main_file"] = target.relative_to(directory).as_posix()
            _json_dump(directory / "project.json", manifest)
            _snapshot(directory, "Initial clone")
        elif template not in {"master-cv", "canonical-resume", "historical-resume", "tldp-resume"}:
            try:
                origin_dir = _managed_dir(cv_root, template)
                for folder in ("source", "assets"):
                    origin = origin_dir / folder
                    if origin.is_dir():
                        shutil.copytree(origin, directory / folder, symlinks=False, dirs_exist_ok=True)
                origin_manifest = _manifest(origin_dir)
                manifest["main_file"] = origin_manifest.get("main_file") or "source/main.tex"
                _json_dump(directory / "project.json", manifest)
                _check_limits(directory)
                _snapshot(directory, "Initial clone")
            except ProjectNotFound:
                pass
    return {"project": manifest}


def archive_project(cv_root: Path, project_id: str, archived: bool = True) -> Dict[str, Any]:
    directory = _managed_dir(cv_root, project_id)
    manifest = _manifest(directory)
    if manifest.get("kind") != "managed":
        raise ProjectForbidden("protected projects cannot be archived")
    manifest["archived_at"] = now_iso() if archived else ""
    manifest["updated_at"] = now_iso()
    _json_dump(directory / "project.json", manifest)
    return {"project": manifest}


def _editable(cv_root: Path, project_id: str) -> Tuple[Path, Dict[str, Any]]:
    try:
        directory = _managed_dir(cv_root, project_id)
    except ProjectNotFound:
        if any(item.get("id") == project_id for item in _legacy_project_specs(cv_root)):
            raise ProjectForbidden("only active private projects are editable")
        raise
    manifest = _manifest(directory)
    if manifest.get("kind") != "managed" or manifest.get("archived_at"):
        raise ProjectForbidden("only active private projects are editable")
    return directory, manifest


def save_file(cv_root: Path, project_id: str, rel: str, content: str, expected_sha256: str = "") -> Dict[str, Any]:
    directory, manifest = _editable(cv_root, project_id)
    rel = validate_file_path(rel)
    if _extension(rel) not in TEXT_EXTENSIONS:
        raise ProjectError("only text sources can be saved as text")
    raw = str(content or "").encode("utf-8")
    if len(raw) > MAX_TEXT_BYTES:
        raise ProjectError("text file exceeds 512 KiB")
    target = _project_file(directory, rel)
    current = sha256_path(target) if target.is_file() else ""
    if target.is_file() and not expected_sha256:
        raise ProjectConflict("previous SHA-256 is required for an existing file")
    if expected_sha256 and current != expected_sha256:
        raise ProjectConflict("file changed on disk; reload before saving")
    _check_write_limits(directory, target, raw)
    _snapshot(directory, "Save %s" % rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp-" + uuid.uuid4().hex)
    tmp.write_bytes(raw)
    os.replace(tmp, target)
    manifest["updated_at"] = now_iso()
    _json_dump(directory / "project.json", manifest)
    _check_limits(directory)
    return {"path": rel, "sha256": sha256_bytes(raw), "size": len(raw), "updated_at": manifest["updated_at"]}


def mutate_file(cv_root: Path, project_id: str, action: str, **kwargs: Any) -> Dict[str, Any]:
    directory, manifest = _editable(cv_root, project_id)
    action = str(action or "")
    if action in {"mkdir", "create_folder"}:
        rel = _safe_path(str(kwargs.get("path") or ""))
        if not (rel.startswith("source/") or rel.startswith("assets/")):
            raise ProjectError("folders must live under source/ or assets/")
        target = _project_any_path(directory, rel)
        if target.exists():
            raise ProjectConflict("folder already exists")
        _snapshot(directory, "Create folder %s" % rel)
        target.mkdir(parents=True, exist_ok=False)
    elif action == "create":
        rel = validate_file_path(str(kwargs.get("path") or ""))
        target = _project_file(directory, rel)
        if target.exists():
            raise ProjectConflict("file already exists")
        content = str(kwargs.get("content") or "")
        raw = content.encode("utf-8")
        if _extension(rel) in TEXT_EXTENSIONS and len(raw) > MAX_TEXT_BYTES:
            raise ProjectError("text file exceeds 512 KiB")
        _check_write_limits(directory, target, raw)
        _snapshot(directory, "Create %s" % rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    elif action == "upload":
        rel = validate_file_path(str(kwargs.get("path") or ""))
        if not rel.startswith("assets/") or _extension(rel) not in ASSET_EXTENSIONS:
            raise ProjectError("uploads must be PNG, JPEG, or PDF assets")
        raw = kwargs.get("data")
        if isinstance(raw, str):
            try:
                raw = base64.b64decode(raw, validate=True)
            except (ValueError, TypeError):
                raise ProjectError("invalid base64 upload")
        if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_FILE_BYTES:
            raise ProjectError("asset exceeds 10 MiB")
        target = _project_file(directory, rel)
        _check_write_limits(directory, target, bytes(raw))
        _snapshot(directory, "Upload %s" % rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(bytes(raw))
    elif action == "rename":
        old = _safe_path(str(kwargs.get("path") or ""))
        new = _safe_path(str(kwargs.get("new_path") or ""))
        source, target = _project_any_path(directory, old), _project_any_path(directory, new)
        if not source.exists() or target.exists():
            raise ProjectError("file not found or destination exists")
        if source.is_file() and _extension(old) != _extension(new):
            raise ProjectError("renaming cannot change the file type")
        _snapshot(directory, "Rename %s" % old)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, target)
        if manifest.get("main_file") == old:
            manifest["main_file"] = new
    elif action == "trash":
        rel = _safe_path(str(kwargs.get("path") or ""))
        target = _project_any_path(directory, rel)
        if not target.exists():
            raise ProjectNotFound("file not found")
        revision = _snapshot(directory, "Trash %s" % rel)
        trash = directory / ".trash" / str(revision["revision_id"]) / rel
        trash.parent.mkdir(parents=True, exist_ok=True)
        os.replace(target, trash)
        if manifest.get("main_file") == rel:
            manifest["main_file"] = ""
    elif action == "set_main":
        rel = validate_file_path(str(kwargs.get("path") or ""))
        if not rel.startswith("source/") or _extension(rel) != ".tex" or not _project_file(directory, rel).is_file():
            raise ProjectError("main document must be an existing source .tex file")
        _snapshot(directory, "Set main document")
        manifest["main_file"] = rel
    else:
        raise ProjectError("unsupported file action")
    manifest["updated_at"] = now_iso()
    _json_dump(directory / "project.json", manifest)
    _check_limits(directory)
    return {"project": manifest, "files": list_project_files(cv_root, project_id)["files"]}


def set_main(cv_root: Path, project_id: str, path: str) -> Dict[str, Any]:
    return mutate_file(cv_root, project_id, "set_main", path=path)


def history(cv_root: Path, project_id: str) -> Dict[str, Any]:
    directory = _managed_dir(cv_root, project_id)
    if _manifest(directory).get("kind") != "managed":
        return {"revisions": []}
    rows = []
    for revision in sorted((directory / "history").glob("*/revision.json"), reverse=True):
        value = _json_load(revision, {}) or {}
        rows.append({k: value.get(k) for k in ("revision_id", "created_at", "label", "files")})
    return {"revisions": rows}


def restore(cv_root: Path, project_id: str, revision_id: str) -> Dict[str, Any]:
    directory, manifest = _editable(cv_root, project_id)
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", str(revision_id or "")):
        raise ProjectError("invalid revision id")
    revision = directory / "history" / revision_id
    snapshot_dir = revision / "snapshot"
    if not snapshot_dir.is_dir():
        raise ProjectNotFound("revision not found")
    _snapshot(directory, "Before restore %s" % revision_id)
    current = {path.relative_to(directory).as_posix() for path in _iter_managed_files(directory)}
    restored = {path.relative_to(snapshot_dir).as_posix() for path in snapshot_dir.rglob("*") if path.is_file()}
    for rel in current - restored:
        path = _project_file(directory, rel)
        trash = directory / ".trash" / ("restore-" + uuid.uuid4().hex[:8]) / rel
        trash.parent.mkdir(parents=True, exist_ok=True)
        os.replace(path, trash)
    for source in snapshot_dir.rglob("*"):
        if source.is_file():
            rel = source.relative_to(snapshot_dir).as_posix()
            target = _project_file(directory, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    revision_meta = _json_load(revision / "revision.json", {}) or {}
    previous_manifest = revision_meta.get("manifest") if isinstance(revision_meta, dict) else None
    if isinstance(previous_manifest, dict) and previous_manifest.get("main_file") is not None:
        manifest["main_file"] = previous_manifest.get("main_file") or ""
    manifest["updated_at"] = now_iso()
    _json_dump(directory / "project.json", manifest)
    _check_limits(directory)
    return {"project": manifest, "restored_revision": revision_id, "files": list_project_files(cv_root, project_id)["files"]}


def compile_project(cv_root: Path, project_id: str) -> Dict[str, Any]:
    directory, manifest = _editable(cv_root, project_id)
    _check_limits(directory)
    main_file = str(manifest.get("main_file") or "")
    if not main_file or not main_file.startswith("source/") or _extension(main_file) != ".tex":
        raise ProjectError("set a source .tex main document before compiling")
    source = _project_file(directory, main_file)
    if not source.is_file():
        raise ProjectNotFound("main document not found")
    _snapshot(directory, "Build snapshot")
    build_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
    generated = directory / "generated" / build_id
    generated.mkdir(parents=True, exist_ok=False)
    status = {"build_id": build_id, "project_id": project_id, "status": "running", "started_at": now_iso(), "main_file": main_file, "draft": True}
    _json_dump(generated / "build.json", status)
    log = ""
    try:
        with tempfile.TemporaryDirectory(prefix="resume-studio-build-") as temp_name:
            stage = Path(temp_name)
            for folder in ("source", "assets"):
                src = directory / folder
                if src.is_dir():
                    shutil.copytree(src, stage / folder, symlinks=False)
            out = stage / "out"
            out.mkdir()
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin:/opt/homebrew/bin"),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "HOME": tempfile.mkdtemp(prefix="resume-studio-home-"),
            }
            command = ["tectonic", "--untrusted", "--outdir", str(out), str(stage / main_file)]
            proc = subprocess.run(command, cwd=str(stage), env=env, capture_output=True, text=True, timeout=COMPILE_TIMEOUT_SECONDS)
            log = _sanitize_log((proc.stdout or "") + "\n" + (proc.stderr or ""))
            pdf = out / (Path(main_file).stem + ".pdf")
            if proc.returncode != 0 or not pdf.is_file():
                raise ProjectError("Tectonic failed to compile the main document")
            target_pdf = generated / "workspace_draft.pdf"
            shutil.copy2(pdf, target_pdf)
            pages = None
            try:
                info = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True, timeout=10, check=False)
                match = re.search(r"^Pages:\s+(\d+)", info.stdout or "", re.MULTILINE)
                pages = int(match.group(1)) if match else None
            except (OSError, subprocess.TimeoutExpired, ValueError):
                pass
            preview = generated / "preview.png"
            subprocess.run(["pdftoppm", "-png", "-r", "144", "-singlefile", str(pdf), str(generated / "preview")], capture_output=True, timeout=20, check=False)
            status.update({"status": "complete", "completed_at": now_iso(), "pdf": "generated/%s/workspace_draft.pdf" % build_id, "preview": "generated/%s/preview.png" % build_id, "page_count": pages, "log": log, "artifact_status": "workspace_draft"})
    except subprocess.TimeoutExpired:
        status.update({"status": "failed", "completed_at": now_iso(), "error": "compile timed out", "log": _sanitize_log(log)})
    except (OSError, ProjectError) as exc:
        status.update({"status": "failed", "completed_at": now_iso(), "error": str(exc), "log": _sanitize_log(log)})
    _json_dump(generated / "build.json", status)
    (generated / "compile.log").write_text(_sanitize_log(log), encoding="utf-8")
    manifest["updated_at"] = now_iso()
    _json_dump(directory / "project.json", manifest)
    return status


def build_status(cv_root: Path, project_id: str, build_id: str) -> Dict[str, Any]:
    directory = _managed_dir(cv_root, project_id)
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", str(build_id or "")):
        raise ProjectError("invalid build id")
    value = _json_load(directory / "generated" / build_id / "build.json")
    if not isinstance(value, dict):
        raise ProjectNotFound("build not found")
    return value
