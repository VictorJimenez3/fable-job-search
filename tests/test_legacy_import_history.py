"""Regression coverage for importing compacted legacy job snapshots."""

from __future__ import annotations

import json
import uuid

import pytest

from radar import state
from radar.db.import_legacy import import_legacy, public_id


class CaptureRepository:
    """Small repository double that keeps the importer payloads observable."""

    def __init__(self) -> None:
        self.companies: list[tuple[str, dict]] = []
        self.postings: list[dict] = []
        self.scores: list[dict] = []

    def ensure_profile(self, profile_id: str, display_name: str, config: dict) -> None:
        return None

    def ensure_company(self, display_name: str, metadata: dict | None = None) -> uuid.UUID:
        self.companies.append((display_name, metadata or {}))
        return uuid.uuid5(uuid.NAMESPACE_URL, f"company:{display_name}")

    def upsert_posting(self, record: dict) -> uuid.UUID:
        self.postings.append(record)
        return uuid.uuid5(uuid.NAMESPACE_URL, f"posting:{record['public_id']}")

    def add_alias(self, alias: str, posting_id: uuid.UUID, kind: str) -> None:
        return None

    def ensure_source_board(self, board: dict) -> uuid.UUID:
        return uuid.uuid5(uuid.NAMESPACE_URL, f"board:{board['profile_id']}:{board['tenant']}")

    def upsert_sighting(self, sighting: dict) -> None:
        return None

    def upsert_score(self, snapshot: dict) -> None:
        self.scores.append(snapshot)


def _record(*, profile: str, legacy_id: str, status: str, score: int, source: str,
            sector: str, ats: str) -> dict:
    return {
        "id": legacy_id,
        "company": "Acme Health",
        "title": "Software Engineer",
        "url": f"https://jobs.example.test/{legacy_id}",
        "locations": ["Remote"],
        "remote": True,
        "salary": "$120,000",
        "source": source,
        "source_board": f"ats:{source}:acme",
        "source_url": f"https://{source}.example.test/acme",
        "ats": ats,
        "sector": sector,
        "posting": {"years_min": 0, "visa": "sponsorship possible"},
        "posted_at": 90,
        "first_seen": 100,
        "last_seen_at": 200,
        "posting_status": status,
        "posting_status_reason": "source closed" if status != "open" else "",
        "posting_status_changed_at": 300 if status != "open" else None,
        "closed_at": 300 if status != "open" else None,
        "profile": profile,
        "score": score,
        "evidence_score": score,
        "score_raw": score - 0.5,
        "score_version": 15,
        "score_dimensions": {"role_fit": 20, "mission": 6},
        "score_reasons": ["role:swe +20", "healthcare preference +6"],
        "alert_ok": status == "open",
    }


@pytest.mark.parametrize(
    ("namespace", "jobs_name", "profile_id"),
    [("new_grad", "jobs.json", "new_grad"), ("internship", "intern_jobs.json", "internship")],
)
def test_import_legacy_hydrates_history_for_both_job_lanes(
    tmp_path, monkeypatch, namespace, jobs_name, profile_id
):
    state_dir = tmp_path / "state"
    monkeypatch.setattr(state, "STATE_DIR", state_dir)
    closed_id = f"{profile_id}-closed"
    reopened_id = f"{profile_id}-reopened"
    closed = _record(
        profile=profile_id, legacy_id=closed_id, status="expired", score=88,
        source="greenhouse", sector="healthcare", ats="greenhouse",
    )
    reopened = _record(
        profile=profile_id, legacy_id=reopened_id, status="open", score=81,
        source="ashby", sector="software", ats="ashby",
    )
    state.save("jobs.json", {closed_id: closed, reopened_id: reopened}, namespace=namespace)

    # A stale terminal history row must not overwrite a posting that has since reopened.
    history_name = jobs_name.replace("jobs.json", "jobs_history.json")
    history_path = state_dir / history_name
    history = json.loads(history_path.read_text())
    history[reopened_id] = _record(
        profile=profile_id, legacy_id=reopened_id, status="expired", score=5,
        source="legacy-source", sector="stale-sector", ats="legacy-ats",
    )
    history_path.write_text(json.dumps(history))

    repo = CaptureRepository()
    totals = import_legacy(root=tmp_path, repo=repo)

    assert totals["postings"] == 2
    imported = {record["metadata"]["legacy_id"]: record for record in repo.postings}

    closed_posting = imported[closed_id]
    assert closed_posting["profile_id"] == profile_id
    assert closed_posting["public_id"] == public_id(profile_id, {**closed, "id": closed_id})
    assert closed_posting["sector"] == "healthcare"
    assert closed_posting["posting_facts"] == closed["posting"]
    assert closed_posting["metadata"]["source"] == "greenhouse"
    assert closed_posting["metadata"]["ats"] == "greenhouse"

    reopened_posting = imported[reopened_id]
    assert reopened_posting["sector"] == "software"
    assert reopened_posting["metadata"]["source"] == "ashby"
    assert reopened_posting["metadata"]["ats"] == "ashby"
    assert reopened_posting["public_id"] == public_id(profile_id, {**reopened, "id": reopened_id})

    closed_score = next(
        snapshot for snapshot in repo.scores
        if snapshot["evidence_score"] == closed["evidence_score"]
    )
    assert closed_score["dimensions"] == closed["score_dimensions"]
    assert closed_score["reasons"] == closed["score_reasons"]
    assert closed_score["version"] == str(closed["score_version"])
