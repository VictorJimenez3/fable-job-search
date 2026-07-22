import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


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
