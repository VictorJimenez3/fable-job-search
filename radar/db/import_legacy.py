"""Idempotent JSON-to-Postgres migration and parity reports."""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from radar.config import ROOT

from .repository import RadarRepository, repository
from .schema import application_events, applications, postings


def public_id(profile_id: str, record: dict[str, Any]) -> str:
    namespace = str(record.get("ats") or record.get("source") or "legacy").casefold()
    external = str(record.get("external_id") or record.get("url") or record.get("id") or "")
    digest = hashlib.sha256(f"{profile_id}|{namespace}|{external}".encode()).digest()[:16]
    return "job_" + base64.b32encode(digest).decode().rstrip("=").lower()


def _at(value: Any, fallback: datetime) -> datetime:
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (TypeError, ValueError, OverflowError):
        return fallback


def import_legacy(root: Path = ROOT, repo: RadarRepository | None = None) -> dict[str, Any]:
    repo = repo or repository()
    now = datetime.now(UTC)
    totals: dict[str, Any] = {
        "postings": 0,
        "applications": 0,
        "aliases": 0,
        "rejected": [],
    }
    profiles_to_files = {
        "new_grad": ("jobs.json", "applied.json"),
        "internship": ("intern_jobs.json", "intern_applied.json"),
    }
    for profile_id, (jobs_name, applications_name) in profiles_to_files.items():
        repo.ensure_profile(profile_id, profile_id.replace("_", " ").title(), {})
        jobs_path = root / "state" / jobs_name
        records = json.loads(jobs_path.read_text()) if jobs_path.exists() else {}
        for legacy_id, value in records.items():
            try:
                seen = _at(value.get("last_seen_at") or value.get("posted_at"), now)
                company_name = str(value.get("company") or "Unknown")[:240]
                company_id = repo.ensure_company(
                    company_name,
                    {"sector": str(value.get("sector") or "")[:80]},
                )
                posting_uuid = repo.upsert_posting(
                    {
                        "public_id": public_id(profile_id, {**value, "id": legacy_id}),
                        "profile_id": profile_id,
                        "company_id": company_id,
                        "company": company_name,
                        "title": str(value.get("title") or "Untitled")[:500],
                        "canonical_url": str(value.get("url") or ""),
                        "locations": value.get("locations") or [],
                        "remote": bool(value.get("remote")),
                        "salary": str(value.get("salary") or ""),
                        "sector": str(value.get("sector") or ""),
                        "posted_at": _at(value.get("posted_at"), now) if value.get("posted_at") else None,
                        "first_seen_at": _at(
                            value.get("first_seen")
                            or value.get("first_seen_at")
                            or value.get("posted_at"),
                            seen,
                        ),
                        "last_seen_at": seen,
                        "status": str(value.get("posting_status") or "open")
                        if str(value.get("posting_status") or "open") in {"open", "expired", "filled", "archived"}
                        else "archived",
                        "status_reason": str(value.get("posting_status_reason") or ""),
                        "posting_facts": value.get("posting") or {},
                        "metadata": {
                            "legacy_id": legacy_id,
                            "source": value.get("source"),
                            "source_board": value.get("source_board"),
                            "source_board_variants": value.get("source_board_variants") or [],
                            "ats": value.get("ats"),
                            "link_resolution": value.get("link_resolution") or {},
                        },
                    }
                )
                repo.add_alias(str(legacy_id), posting_uuid, "legacy_id")
                source = str(value.get("source") or value.get("ats") or "legacy")[:80]
                board_identity = str(
                    value.get("source_board")
                    or f"legacy:{source}:{value.get('source_url') or value.get('ats') or legacy_id}"
                )[:240]
                board_id = repo.ensure_source_board(
                    {
                        "namespace": source,
                        # The full board key, not merely the ATS family, is the
                        # lifecycle boundary (for example Greenhouse + tenant).
                        "tenant": board_identity,
                        "board_url": str(value.get("source_url") or "") or None,
                        "profile_id": profile_id,
                        "enabled": True,
                    }
                )
                payload_hash = hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()
                repo.upsert_sighting(
                    {
                        "posting_id": posting_uuid,
                        "board_id": board_id,
                        "external_id": str(value.get("external_id") or legacy_id)[:300],
                        "source_url": str(value.get("source_url") or value.get("url") or ""),
                        "payload_hash": payload_hash,
                        "first_seen_at": _at(
                            value.get("first_seen")
                            or value.get("first_seen_at")
                            or value.get("posted_at"),
                            seen,
                        ),
                        "last_seen_at": seen,
                    }
                )
                reasons = value.get("score_reasons") if isinstance(value.get("score_reasons"), list) else []
                score_value = max(
                    0, min(100, int(value.get("evidence_score", value.get("score")) or 0))
                )
                derived_eligible = (
                    "eligible"
                    if value.get("alert_ok")
                    else "review"
                    if value.get("early_career_possible") or value.get("explicit_new_grad") or score_value >= 45
                    else "excluded"
                )
                eligible = (
                    value.get("eligibility")
                    if value.get("eligibility") in {"eligible", "review", "excluded"}
                    else derived_eligible
                )
                derived_priority = (
                    "goal"
                    if any("goal company" in str(reason).casefold() for reason in reasons)
                    else "recommended"
                    if score_value >= 66
                    else "explore"
                )
                priority = (
                    value.get("priority_tier")
                    if value.get("priority_tier") in {"goal", "recommended", "explore"}
                    else derived_priority
                )
                score_input = {
                    "score": score_value,
                    "score_raw": value.get("score_raw"),
                    "dimensions": value.get("score_dimensions"),
                    "reasons": reasons,
                    "alert_ok": value.get("alert_ok"),
                }
                repo.upsert_score(
                    {
                        "posting_id": posting_uuid,
                        "profile_id": profile_id,
                        "version": str(value.get("rules_version") or value.get("score_version") or "legacy-v1")[:40],
                        "input_hash": hashlib.sha256(
                            json.dumps(score_input, sort_keys=True, default=str).encode()
                        ).hexdigest(),
                        "evidence_score": score_value,
                        "raw_score": float(value.get("score_raw") or score_value),
                        "eligibility": eligible,
                        "priority_tier": priority,
                        "dimensions": value.get("score_dimensions") or {},
                        "reasons": reasons,
                    }
                )
                totals["postings"] += 1
                totals["aliases"] += 1
            except Exception as exc:
                totals["rejected"].append({"profile": profile_id, "id": legacy_id, "error": str(exc)[:240]})
        app_path = root / "state" / applications_name
        entries = json.loads(app_path.read_text()) if app_path.exists() else []
        for index, entry in enumerate(entries):
            legacy_id = str(entry.get("id") or "")
            resolved = repo.resolve_posting(legacy_id) if legacy_id else None
            occurred = _at(entry.get("updated_at") or entry.get("applied_at") or entry.get("saved_at"), now)
            app_id = uuid.uuid5(uuid.NAMESPACE_URL, f"job-radar:{profile_id}:{legacy_id or index}")
            stage = str(entry.get("stage") or "saved").casefold()
            if stage not in {
                "saved",
                "applied",
                "oa",
                "interview",
                "offer",
                "rejected",
                "withdrawn",
                "closed",
                "not_pursuing",
            }:
                stage = "saved"
            repo.record_application_event(
                {
                    "id": app_id,
                    "posting_id": resolved.get("id") if resolved else None,
                    "profile_id": profile_id,
                    "current_stage": stage,
                    "company": str(entry.get("company") or (resolved or {}).get("company") or "Unknown")[:240],
                    "title": str(entry.get("title") or (resolved or {}).get("title") or "Untitled")[:500],
                    "url": str(entry.get("url") or (resolved or {}).get("canonical_url") or ""),
                    "external_links": {
                        key: entry.get(key) for key in ("notion_page_id", "sheet_row") if entry.get(key)
                    },
                    "created_at": occurred,
                    "updated_at": occurred,
                },
                {
                    "stage": stage,
                    "origin": str(entry.get("via") or entry.get("origin") or "legacy-import")[:80],
                    "idempotency_key": f"legacy:{profile_id}:{legacy_id or index}:{stage}",
                    "metadata": {"legacy": entry},
                    "occurred_at": occurred,
                },
            )
            totals["applications"] += 1
    return totals


def parity(root: Path = ROOT, repo: RadarRepository | None = None) -> dict[str, Any]:
    repo = repo or repository()
    legacy_jobs = 0
    legacy_events = 0
    hashes: dict[str, str] = {}
    posting_ids: list[str] = []
    event_keys: list[str] = []
    profile_for_file = {"jobs.json": "new_grad", "intern_jobs.json": "internship"}
    applications_for_file = {"applied.json": "new_grad", "intern_applied.json": "internship"}
    for filename in ("jobs.json", "intern_jobs.json", "applied.json", "intern_applied.json"):
        path = root / "state" / filename
        if not path.exists():
            continue
        raw = path.read_bytes()
        value = json.loads(raw)
        hashes[filename] = hashlib.sha256(raw).hexdigest()
        if "jobs" in filename:
            legacy_jobs += len(value)
            profile_id = profile_for_file[filename]
            posting_ids.extend(
                public_id(profile_id, {**record, "id": legacy_id})
                for legacy_id, record in value.items()
            )
        else:
            legacy_events += len(value)
            profile_id = applications_for_file[filename]
            for index, entry in enumerate(value):
                stage = str(entry.get("stage") or "saved").casefold()
                if stage not in {
                    "saved", "applied", "oa", "interview", "offer", "rejected",
                    "withdrawn", "closed", "not_pursuing",
                }:
                    stage = "saved"
                event_keys.append(
                    f"legacy:{profile_id}:{entry.get('id') or index}:{stage}"
                )
    with repo.engine.connect() as connection:
        db_jobs = connection.execute(select(func.count()).select_from(postings)).scalar_one()
        db_apps = connection.execute(select(func.count()).select_from(applications)).scalar_one()
        db_posting_ids = list(connection.execute(select(postings.c.public_id)).scalars())
        db_event_keys = list(
            connection.execute(
                select(application_events.c.idempotency_key).where(
                    application_events.c.idempotency_key.startswith("legacy:")
                )
            ).scalars()
        )
    def identity_hash(values: list[str]) -> str:
        return hashlib.sha256("\n".join(sorted(values)).encode()).hexdigest()

    legacy_posting_hash = identity_hash(posting_ids)
    db_posting_hash = identity_hash(db_posting_ids)
    legacy_event_hash = identity_hash(event_keys)
    db_event_hash = identity_hash(db_event_keys)
    return {
        "legacy": {
            "postings": legacy_jobs,
            "application_events": legacy_events,
            "posting_identity_hash": legacy_posting_hash,
            "application_event_hash": legacy_event_hash,
            "source_hashes": hashes,
        },
        "postgres": {
            "postings": db_jobs,
            "normalized_applications": db_apps,
            "application_events": len(db_event_keys),
            "posting_identity_hash": db_posting_hash,
            "application_event_hash": db_event_hash,
        },
        "matches": {
            "postings": legacy_jobs == db_jobs,
            "posting_identity_hash": legacy_posting_hash == db_posting_hash,
            "application_events": legacy_events == len(db_event_keys),
            "application_event_hash": legacy_event_hash == db_event_hash,
        },
    }
