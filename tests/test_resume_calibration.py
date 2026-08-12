import json
from pathlib import Path

from scripts import resume_calibration as calibration


def _job(job_id, company, title, score=80):
    return {
        "id": job_id,
        "company": company,
        "title": title,
        "score": score,
        "url": "https://example.com/%s" % job_id,
        "locations": [],
    }


def test_role_family_prefers_specialized_ai_over_generic_swe():
    assert calibration.role_family(_job("1", "NVIDIA", "Deep Learning Software Engineer")) == "specialized_ai"


def test_calibration_pdf_filename_includes_company_and_use_case():
    job = _job("1", "NVIDIA", "Deep Learning Software Engineer")
    job["calibration_role_family"] = "specialized_ai"
    assert calibration.calibration_pdf_filename(job) == "nvidia_specialized_ai_resume.pdf"


def test_varied_selection_covers_families_and_avoids_first_pass_company_duplicates():
    jobs = [
        _job("1", "NVIDIA", "Deep Learning Software Engineer", 100),
        _job("2", "NVIDIA", "Software Engineer", 99),
        _job("3", "Mayo", "Data Scientist", 98),
        _job("4", "Medpace", "Data Engineer", 97),
        _job("5", "Tesla", "Cloud Engineer", 96),
        _job("6", "Bot Auto", "Computer Vision Engineer", 95),
        _job("7", "Booz Allen", "AI Engineer", 94),
        _job("8", "AppCo", "Backend Engineer", 93),
    ]
    selected = calibration.select_varied_jobs(jobs, count=6)
    assert len(selected) == 6
    assert {job["calibration_role_family"] for job in selected} == {
        "data_engineering",
        "data_science",
        "ml_ai_engineering",
        "specialized_ai",
        "cloud_devops",
        "software_engineering",
    }
    first_six_companies = [job["company"] for job in selected]
    assert len(first_six_companies) == len(set(first_six_companies))


def test_feedback_and_artifact_paths_stay_inside_calibration_dir(tmp_path):
    root = tmp_path
    batch = calibration.calibration_root(root) / "batch-1"
    run = batch / "runs" / "case-1"
    run.mkdir(parents=True)
    pdf_name = "example_data_science_resume.pdf"
    (run / pdf_name).write_bytes(b"pdf")
    case = {
        "case_id": "case-1",
        "batch_id": "batch-1",
        "run_dir": "calibration/batch-1/runs/case-1",
        "pdf_filename": pdf_name,
        "preview_filename": "example_data_science_resume-preview.png",
        "job": {"company": "Example", "title": "Data Engineer"},
    }
    batch.mkdir(parents=True, exist_ok=True)
    (batch / "index.json").write_text(json.dumps({"cases": [case]}))

    target = calibration._artifact_path(root, case, pdf_name)
    assert target == (run / pdf_name).resolve()
    assert calibration._artifact_path(root, case, "resume.tex") is None

    saved = calibration.save_feedback(
        root,
        {"batch_id": "batch-1", "case_id": "case-1", "overall": "revise", "notes": "too generic"},
    )
    assert saved["overall"] == "revise"
    lines = (batch / "feedback.jsonl").read_text().splitlines()
    assert len(lines) == 1
