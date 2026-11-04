"""Idempotent JSON-to-Postgres migration and parity reports."""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import func, select

from radar.config import ROOT
from radar.identity import canonical_url
from radar.link_resolver import is_aggregator_url
from radar.state import load_job_snapshot

from .repository import RadarRepository, repository
from .schema import application_events, applications, postings

_TRACKER_AGGREGATOR_HOSTS = frozenset({
    "jobright.ai", "simplify.jobs", "indeed.com", "linkedin.com",
})


def _is_tracker_aggregator_url(url: str | None) -> bool:
    try:
        host = (urlsplit(str(url or "")).hostname or "").lower().rstrip(".")
    except ValueError:
        host = ""
    return is_aggregator_url(url) or host in _TRACKER_AGGREGATOR_HOSTS or any(
        host.endswith("." + base) for base in _TRACKER_AGGREGATOR_HOSTS
    )


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


def _legacy_posting_links(record: dict[str, Any]) -> tuple[str, list[str]]:
    """Normalize a legacy posting URL and retain resolver provenance."""
    source_raw = str(record.get("url") or "").strip()
    source_url = canonical_url(source_raw)
    resolution = record.get("link_resolution") or {}
    resolved_raw = str(resolution.get("resolved_url") or "").strip()
    resolved_url = canonical_url(resolved_raw)
    primary = (
        resolved_url
        if resolution.get("status") != "source_only"
        and resolved_url
        and not _is_tracker_aggregator_url(resolved_url)
        else source_url
    )
    alternates = list(record.get("alternate_urls") or [])
    alternate_keys = {canonical_url(value) for value in alternates if canonical_url(value)}
    primary = primary or resolved_url
    for candidate in (source_raw, resolved_raw, resolution.get("original_url")):
        key = canonical_url(candidate)
        if key and key != primary and key not in alternate_keys:
            alternates.append(candidate)
            alternate_keys.add(key)
    return primary, alternates


def _legacy_application_links(entry: dict[str, Any], resolved: dict | None) -> tuple[str, list[str]]:
    """Choose a safe application URL and retain discovery/manual alternatives."""
    resolved = resolved or {}
    metadata = resolved.get("metadata") or {}
    resolution = metadata.get("link_resolution") or resolved.get("link_resolution") or {}
    resolved_record = {
        "url": resolved.get("canonical_url"),
        "link_resolution": resolution,
        "alternate_urls": metadata.get("alternate_urls") or [],
    }
    resolved_url, resolved_alternates = _legacy_posting_links(resolved_record)
    entry_raw = str(entry.get("url") or "").strip()
    entry_key = canonical_url(entry_raw)
    entry_alternates = list(entry.get("alternate_urls") or [])
    entry_url = entry_key
    if entry_key and _is_tracker_aggregator_url(entry_key):
        entry_url, resolver_alternates = _legacy_posting_links(entry)
        entry_alternates.extend(resolver_alternates)
    # An owner-entered direct/manual URL remains primary. A legacy aggregator
    # URL is replaced by the normalized resolved posting URL.
    if resolved_url and (not entry_key or _is_tracker_aggregator_url(entry_key)):
        primary = resolved_url
    else:
        primary = entry_url or resolved_url
    alternates = list(entry_alternates)
    alternate_keys = {canonical_url(value) for value in alternates if canonical_url(value)}
    for candidate in [*resolved_alternates, resolved_url]:
        key = canonical_url(candidate)
        if key and key != primary and key not in alternate_keys:
            alternates.append(candidate)
            alternate_keys.add(key)
    return primary, alternates


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
        records = load_job_snapshot(jobs_path, {}) if jobs_path.exists() else {}
        for legacy_id, value in records.items():
            try:
                seen = _at(value.get("last_seen_at") or value.get("posted_at"), now)
                company_name = str(value.get("company") or "Unknown")[:240]
                company_id = repo.ensure_company(
                    company_name,
                    {"sector": str(value.get("sector") or "")[:80]},
                )
                posting_metadata = dict(value.get("metadata") or {}) if isinstance(value.get("metadata"), dict) else {}
                location_eligibility = value.get("location_eligibility")
                posting_metadata["location_eligibility"] = (
                    location_eligibility if isinstance(location_eligibility, dict) else {}
                )
                try:
                    ranking_score = max(0.0, min(100.0, float(value.get("score") or 0)))
                except (TypeError, ValueError):
                    ranking_score = 0.0
                posting_url, posting_alternates = _legacy_posting_links(value)
                posting_metadata["ranking_score"] = ranking_score
                if posting_alternates:
                    posting_metadata["alternate_urls"] = posting_alternates
                posting_metadata.update({
                    "legacy_id": legacy_id,
                    "source": value.get("source"),
                    "source_board": value.get("source_board"),
                    "source_board_variants": value.get("source_board_variants") or [],
                    "ats": value.get("ats"),
                    "link_resolution": value.get("link_resolution") or {},
                })
                posting_uuid = repo.upsert_posting(
                    {
                        "public_id": public_id(profile_id, {**value, "id": legacy_id}),
                        "profile_id": profile_id,
                        "company_id": company_id,
                        "company": company_name,
                        "title": str(value.get("title") or "Untitled")[:500],
                        "canonical_url": posting_url,
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
                        "metadata": posting_metadata,
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
                    "location_eligibility": location_eligibility,
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
                "to_tailor",
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
            application_url, alternate_urls = _legacy_application_links(entry, resolved)
            external_links = {
                key: entry.get(key) for key in ("notion_page_id", "sheet_row") if entry.get(key)
            }
            if alternate_urls:
                external_links["alternate_urls"] = alternate_urls
            repo.record_application_event(
                {
                    "id": app_id,
                    "posting_id": resolved.get("id") if resolved else None,
                    "profile_id": profile_id,
                    "current_stage": stage,
                    "company": str(entry.get("company") or (resolved or {}).get("company") or "Unknown")[:240],
                    "title": str(entry.get("title") or (resolved or {}).get("title") or "Untitled")[:500],
                    "url": application_url,
                    "external_links": external_links,
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
                    "saved", "to_tailor", "applied", "oa", "interview", "offer", "rejected",
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
