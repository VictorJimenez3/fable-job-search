import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _detail_pointer(lane: str, job_id: str, detail: dict) -> dict:
    payload = json.dumps(detail, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode()).hexdigest()
    return {
        "version": 1,
        "shard": f"{lane}/{hashlib.sha256(job_id.encode()).hexdigest()[:2]}.json",
        "key": job_id,
        "sha256": digest,
    }


def test_crawl_merge_keeps_new_discoveries_and_upstream_updates(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    upstream = {"known": {"id": "known", "score": 100, "quality": {"fresh": True}}}
    (state_dir / "jobs.json").write_text(json.dumps(upstream))
    (state_dir / "companies.json").write_text(json.dumps({"upstream": {"status": "active"}}))
    (state_dir / "alert_history.json").write_text(json.dumps([{"id": "known", "alerted_at": 1}]))
    (state_dir / "runs.json").write_text(json.dumps([{"ts": 1, "new_jobs": 1, "alerts": 1}]))

    ours_jobs = {
        "known": {"id": "known", "score": 50},
        "discovery": {"id": "discovery", "score": 90},
    }
    paths = {}
    for name, value in {
        "jobs": ours_jobs,
        "companies": {"upstream": {"status": "invalid"}, "new": {"status": "active"}},
        "alerts": [{"id": "known", "alerted_at": 0}, {"id": "discovery", "alerted_at": 2}],
        "runs": [{"ts": 1, "new_jobs": 1, "alerts": 1}, {"ts": 2, "new_jobs": 1, "alerts": 1}],
    }.items():
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(value))
        paths[name] = path

    env = os.environ | {
        "MERGE_CRAWL_JOBS": str(paths["jobs"]),
        "MERGE_CRAWL_COMPANIES": str(paths["companies"]),
        "MERGE_CRAWL_ALERT_HISTORY": str(paths["alerts"]),
        "MERGE_CRAWL_RUNS": str(paths["runs"]),
    }
    subprocess.run([sys.executable, str(ROOT / "scripts/mac-companion/merge_crawl_state.py")],
                   cwd=tmp_path, env=env, check=True)

    jobs = json.loads((state_dir / "jobs.json").read_text())
    assert jobs["known"]["score"] == 100  # fresh upstream wins existing IDs
    assert jobs["discovery"]["score"] == 90
    companies = json.loads((state_dir / "companies.json").read_text())
    assert companies["upstream"]["status"] == "active"
    assert companies["new"]["status"] == "active"
    assert {row["id"] for row in json.loads((state_dir / "alert_history.json").read_text())} == {
        "known", "discovery"
    }


def test_crawl_merge_keeps_full_history_for_new_closed_discoveries(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "jobs.json").write_text(json.dumps({
        "known": {"id": "known", "posting_status": "expired", "score": 90}
    }))
    (state_dir / "jobs_history.json").write_text(json.dumps({
        "known": {"id": "known", "posting_status": "expired", "score_reasons": ["upstream"]}
    }))
    for name, value in (("companies", {}), ("alert_history", []), ("runs", [])):
        (state_dir / f"{name}.json").write_text(json.dumps(value))
    snapshots = {
        "jobs": {"known": {"id": "known", "score": 10},
                 "new_closed": {"id": "new_closed", "posting_status": "expired", "score": 60}},
        "history": {"new_closed": {"id": "new_closed", "posting_status": "expired",
                                   "score": 60, "score_reasons": ["new discovery"]}},
        "companies": {}, "alerts": [], "runs": [],
    }
    paths = {}
    for name, value in snapshots.items():
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(value))
        paths[name] = path
    env = os.environ | {
        "MERGE_CRAWL_JOBS": str(paths["jobs"]),
        "MERGE_CRAWL_JOBS_HISTORY": str(paths["history"]),
        "MERGE_CRAWL_COMPANIES": str(paths["companies"]),
        "MERGE_CRAWL_ALERT_HISTORY": str(paths["alerts"]),
        "MERGE_CRAWL_RUNS": str(paths["runs"]),
    }
    subprocess.run([sys.executable, str(ROOT / "scripts/mac-companion/merge_crawl_state.py")],
                   cwd=tmp_path, env=env, check=True)
    history = json.loads((state_dir / "jobs_history.json").read_text())
    assert history["known"]["score_reasons"] == ["upstream"]
    assert history["new_closed"]["score_reasons"] == ["new discovery"]


def test_crawl_merge_hydrates_and_republishes_referenced_detail_shards(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    for name, value in (("companies.json", {}), ("alert_history.json", []), ("runs.json", [])):
        (state_dir / name).write_text(json.dumps(value))

    upstream_detail = {"score_reasons": ["upstream truth"]}
    upstream_ref = _detail_pointer("jobs_details", "known", upstream_detail)
    (state_dir / "jobs.json").write_text(json.dumps({
        "known": {"id": "known", "score_version": 15, "score": 90,
                  "posting_status": "open", "detail_ref": upstream_ref},
    }))
    upstream_shard = state_dir / upstream_ref["shard"]
    upstream_shard.parent.mkdir(parents=True)
    upstream_shard.write_text(json.dumps({
        "schema_version": 1, "records": {"known": upstream_detail},
        "signatures": {"known": upstream_ref["sha256"]},
    }))

    checkpoint = tmp_path / "checkpoint"
    checkpoint.mkdir()
    discovery_detail = {"score_reasons": ["new discovery"]}
    discovery_ref = _detail_pointer("jobs_details", "discovery", discovery_detail)
    (checkpoint / "jobs.json").write_text(json.dumps({
        "known": {"id": "known", "score_version": 15, "score": 50,
                  "posting_status": "open", "detail_ref": upstream_ref},
        "discovery": {"id": "discovery", "score_version": 15, "score": 80,
                      "posting_status": "open", "detail_ref": discovery_ref},
    }))
    checkpoint_shard = checkpoint / discovery_ref["shard"]
    checkpoint_shard.parent.mkdir(parents=True)
    checkpoint_shard.write_text(json.dumps({
        "schema_version": 1, "records": {"discovery": discovery_detail},
        "signatures": {"discovery": discovery_ref["sha256"]},
    }))

    paths = {}
    for name, value in (("companies", {}), ("alerts", []), ("runs", [])):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(value))
        paths[name] = path
    env = os.environ | {
        "MERGE_CRAWL_JOBS": str(checkpoint / "jobs.json"),
        "MERGE_CRAWL_COMPANIES": str(paths["companies"]),
        "MERGE_CRAWL_ALERT_HISTORY": str(paths["alerts"]),
        "MERGE_CRAWL_RUNS": str(paths["runs"]),
    }
    subprocess.run([sys.executable, str(ROOT / "scripts/mac-companion/merge_crawl_state.py")],
                   cwd=tmp_path, env=env, check=True)

    merged = json.loads((state_dir / "jobs.json").read_text())
    assert merged["known"]["score"] == 90
    assert "detail_ref" in merged["discovery"]
    shard = json.loads((state_dir / merged["discovery"]["detail_ref"]["shard"]).read_text())
    assert shard["records"]["discovery"] == discovery_detail


def test_crawl_merge_is_lane_scoped_for_internship_shards_and_history(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    upstream_detail = {"score_reasons": ["upstream internship"]}
    upstream_ref = _detail_pointer("intern_jobs_details", "known", upstream_detail)
    (state_dir / "intern_jobs.json").write_text(json.dumps({
        "known": {"id": "known", "score_version": 7, "score": 90,
                  "posting_status": "open", "detail_ref": upstream_ref},
    }))
    (state_dir / "intern_jobs_history.json").write_text(json.dumps({}))
    upstream_shard = state_dir / upstream_ref["shard"]
    upstream_shard.parent.mkdir(parents=True)
    upstream_shard.write_text(json.dumps({
        "schema_version": 1, "records": {"known": upstream_detail},
        "signatures": {"known": upstream_ref["sha256"]},
    }))
    for name, value in (("intern_companies", {}), ("intern_alert_history", []),
                        ("intern_runs", []), ("intern_job_aliases", {})):
        (state_dir / f"{name}.json").write_text(json.dumps(value))

    checkpoint = tmp_path / "checkpoint"
    checkpoint.mkdir()
    new_detail = {"score_reasons": ["new internship"]}
    new_ref = _detail_pointer("intern_jobs_details", "new", new_detail)
    (checkpoint / "intern_jobs.json").write_text(json.dumps({
        "new": {"id": "new", "score_version": 7, "score": 80,
                "posting_status": "open", "detail_ref": new_ref},
    }))
    checkpoint_shard = checkpoint / new_ref["shard"]
    checkpoint_shard.parent.mkdir(parents=True)
    checkpoint_shard.write_text(json.dumps({
        "schema_version": 1, "records": {"new": new_detail},
        "signatures": {"new": new_ref["sha256"]},
    }))
    for name, value in (("companies", {}), ("alerts", []), ("runs", []), ("aliases", {})):
        (checkpoint / f"intern_{name}.json").write_text(json.dumps(value))

    env = os.environ | {
        "RADAR_PROFILE": "internship",
        "MERGE_CRAWL_JOBS": str(checkpoint / "intern_jobs.json"),
        "MERGE_CRAWL_JOBS_HISTORY": str(checkpoint / "intern_jobs_history.json"),
        "MERGE_CRAWL_COMPANIES": str(checkpoint / "intern_companies.json"),
        "MERGE_CRAWL_ALERT_HISTORY": str(checkpoint / "intern_alerts.json"),
        "MERGE_CRAWL_RUNS": str(checkpoint / "intern_runs.json"),
        "MERGE_CRAWL_ALIASES": str(checkpoint / "intern_aliases.json"),
    }
    subprocess.run([sys.executable, str(ROOT / "scripts/mac-companion/merge_crawl_state.py")],
                   cwd=tmp_path, env=env, check=True)

    merged = json.loads((state_dir / "intern_jobs.json").read_text())
    assert set(merged) == {"known", "new"}
    shard = json.loads((state_dir / merged["new"]["detail_ref"]["shard"]).read_text())
    assert shard["records"]["new"] == new_detail
    assert not (state_dir / "jobs.json").exists()
    assert not (state_dir / "jobs_details").exists()


def test_enrich_merge_keeps_upstream_truth_and_adds_hydrated_quality_cache(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    culture = tmp_path / "culture.json"
    culture.write_text("{}")
    state_culture = state_dir / "culture.json"
    state_culture.write_text("{}")
    for name, value in (("company_research.json", {}), ("ai_usage.json", {})):
        (state_dir / name).write_text(json.dumps(value))

    upstream_detail = {"score_reasons": ["current reasons"]}
    upstream_ref = _detail_pointer("jobs_details", "known", upstream_detail)
    (state_dir / "jobs.json").write_text(json.dumps({
        "known": {"id": "known", "score_version": 15, "score": 90,
                  "posting_status": "open", "detail_ref": upstream_ref,
                  "quality": {"reason": "upstream"}},
    }))
    upstream_shard = state_dir / upstream_ref["shard"]
    upstream_shard.parent.mkdir(parents=True)
    upstream_shard.write_text(json.dumps({
        "schema_version": 1, "records": {"known": upstream_detail},
        "signatures": {"known": upstream_ref["sha256"]},
    }))

    checkpoint = tmp_path / "checkpoint"
    checkpoint.mkdir()
    ours_detail = {"score_reasons": ["new job reasons"]}
    ours_ref = _detail_pointer("jobs_details", "new", ours_detail)
    (checkpoint / "jobs.json").write_text(json.dumps({
        "known": {"id": "known", "score_version": 15, "score": 50,
                  "posting_status": "open", "detail_ref": upstream_ref,
                  "quality": {"reason": "ours should not replace upstream"}},
        "new": {"id": "new", "score_version": 15, "score": 70,
                "posting_status": "open", "detail_ref": ours_ref,
                "quality": {"reason": "ours cache"}},
    }))
    ours_shard = checkpoint / ours_ref["shard"]
    ours_shard.parent.mkdir(parents=True)
    ours_shard.write_text(json.dumps({
        "schema_version": 1, "records": {"new": ours_detail},
        "signatures": {"new": ours_ref["sha256"]},
    }))

    env = os.environ | {
        "MERGE_JOBS": str(checkpoint / "jobs.json"),
        "MERGE_CULTURE": str(culture),
    }
    subprocess.run([sys.executable, str(ROOT / "scripts/mac-companion/merge_state.py")],
                   cwd=tmp_path, env=env, check=True)

    merged = json.loads((state_dir / "jobs.json").read_text())
    assert merged["known"]["quality"] == {"reason": "upstream"}
    assert merged["new"]["quality"] == {"reason": "ours cache"}
    new_detail = json.loads((state_dir / merged["new"]["detail_ref"]["shard"]).read_text())
    assert new_detail["records"]["new"] == ours_detail
