import hashlib
from pathlib import Path

import pytest

from scripts import resume_projects as projects
from scripts import resume_studio


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def cv(tmp_path):
    immutable = tmp_path / "immutable"
    immutable.mkdir()
    (immutable / "VictorJimenezResume.tex").write_text("\\documentclass{article}")
    (immutable / "VictorJimenezResume.pdf").write_bytes(b"%PDF-immutable")
    (immutable / "og_resume.tex").write_text("old")
    (immutable / "og_resume.pdf").write_bytes(b"%PDF-old")
    (immutable / "tldp_resume.tex").write_text("tldp")
    (immutable / "tldp_resume.pdf").write_bytes(b"%PDF-tldp")
    (tmp_path / "cv_full.tex").write_text("master")
    return tmp_path


def test_bootstrap_is_logical_and_non_destructive(cv):
    before = {path: sha(path) for path in cv.rglob("*") if path.is_file()}
    result = projects.list_projects(cv)
    assert {"canonical-resume", "historical-resume", "tldp-resume", "master-cv"} <= {item["id"] for item in result["projects"]}
    assert {path: sha(path) for path in cv.rglob("*") if path.is_file()} == before


def test_private_project_save_conflict_history_restore_and_trash(cv):
    project_id = projects.create_project(cv, "Private")['project']['id']
    projects.mutate_file(cv, project_id, "create", path="source/main.tex", content="first")
    current = projects.read_file(cv, project_id, "source/main.tex")
    projects.save_file(cv, project_id, "source/main.tex", "second", current["sha256"])
    with pytest.raises(projects.ProjectConflict):
        projects.save_file(cv, project_id, "source/main.tex", "stale", current["sha256"])
    revisions = projects.history(cv, project_id)["revisions"]
    assert len(revisions) >= 2
    projects.restore(cv, project_id, next(row["revision_id"] for row in revisions if "Save" in row.get("label", "")))
    assert projects.read_file(cv, project_id, "source/main.tex")["content"] == "first"
    projects.mutate_file(cv, project_id, "trash", path="source/main.tex")
    assert not (cv / ".resume_studio" / "projects" / project_id / "source" / "main.tex").exists()


def test_validation_limits_and_immutable_origin(cv):
    with pytest.raises(projects.ProjectError):
        projects.read_file(cv, "canonical-resume", "../cv_full.tex")
    with pytest.raises(projects.ProjectForbidden):
        projects.save_file(cv, "canonical-resume", "source/VictorJimenezResume.tex", "changed")
    project_id = projects.create_project(cv, "Limits")['project']['id']
    with pytest.raises(projects.ProjectError):
        projects.mutate_file(cv, project_id, "create", path="source/run.sh", content="echo unsafe")
    with pytest.raises(projects.ProjectError):
        projects.save_file(cv, project_id, "source/main.tex", "x" * (projects.MAX_TEXT_BYTES + 1))
    assert not (cv / ".resume_studio" / "projects" / project_id / "source" / "main.tex").exists()


def test_clone_does_not_modify_origin(cv):
    origin = projects.create_project(cv, "Origin")['project']['id']
    projects.mutate_file(cv, origin, "create", path="source/main.tex", content="origin")
    clone = projects.create_project(cv, "Clone", template=origin)['project']['id']
    projects.save_file(cv, clone, "source/main.tex", "clone", projects.read_file(cv, clone, "source/main.tex")["sha256"])
    assert projects.read_file(cv, origin, "source/main.tex")["content"] == "origin"


def test_project_capability_and_bridge_contract():
    capability = resume_studio.StudioHandler.issue_project_capability()["capability"]
    assert resume_studio.StudioHandler.valid_project_capability(capability)
    assert not resume_studio.StudioHandler.valid_project_capability("not-a-capability")
    assert not resume_studio.StudioHandler.cors_paths("/api/projects")
    assert "project_capability" in resume_studio.UI_HTML
    assert "X-Resume-Project-Capability" in resume_studio.UI_HTML
    assert "owner only · @VictorJimenez3" in resume_studio.UI_HTML
    assert "source stays on the Mac" in resume_studio.UI_HTML
    assert "loadEngineStatus" in resume_studio.UI_HTML
    assert "testing-hero" in resume_studio.UI_HTML
