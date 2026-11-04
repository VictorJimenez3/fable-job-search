"""State lives in small JSON files committed back to the repo by CI.

jobs.json      {job_id: record}                — everything ever seen, with score
companies.json {ats:token: registry entry}     — the self-expanding company registry
feedback.json  {company_boosts, token_boosts, explicit_*, negative, taste_events}
              — explicit feedback; the implicit positive sample is applied.json
applied.json   [{id, company, title, url, applied_at, notion_synced, ...}]
score_preferences.json {enabled_dimensions, version, updated_at}
              — owner-selected optional score sections
runs.json      [{ts, new_jobs, alerts, sources: {...}}]  — last 200 run summaries
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from .config import STATE_DIR, profile_id

# GitHub rejects blobs larger than 100 MiB. Leave enough room for a crawl to
# report and recover before a generated commit reaches that hard boundary.
DEFAULT_MAX_JOB_SNAPSHOT_BYTES = 95 * 1024 * 1024
# Keep each detail shard comfortably below the GitHub blob ceiling even when
# one hash bucket is unusually dense.  The primary snapshot guard remains the
# operational limit for the index; detail files have their own guard so a
# future field cannot simply move the same failure to another blob.
DEFAULT_MAX_JOB_DETAIL_SHARD_BYTES = 8 * 1024 * 1024
_DETAIL_SCHEMA_VERSION = 1
_DETAIL_REF_FIELD = "detail_ref"
_DETAIL_FIELDS = (
    "score_reasons", "eligibility_evidence", "score_dimensions", "score_dimensions_raw",
    "sponsorship_history", "link_resolution", "posting_identity",
    "lifecycle_events", "source_variants", "source_board_variants", "source_url_variants",
    "posting", "lifecycle_checked_at", "posting_status_reason",
    "ranking_adjustment", "score_raw", "score_calibrated", "rules_v", "location_eligibility",
)
_PRIMARY_DETAIL_HINTS = {
    "sponsorship_history": ("status",),
    "link_resolution": ("status",),
    "posting_identity": ("variant_count",),
    "posting": ("sponsorship", "years_min", "intern_counts"),
    "location_eligibility": ("status",),
}
_HARD_GATE_REASON_RE = re.compile(
    r"^(?:intern/co-op/contract|senior\+ title|PhD-targeted title|"
    r"requires clearance|non-US location|requires [1-9]\+ years(?: \(not new-grad\))?|"
    r"not an AI/SWE/DS role)$",
    re.IGNORECASE,
)

# These fields have the same defaults at every read boundary. Persisting them
# on tens of thousands of historical rows adds megabytes without preserving
# information. Keep this list deliberately narrow and compatibility-tested.
_JOB_DEFAULTS = {
    "description": "",
    "llm_note": "",
    "salary": "",
    "remote": False,
    "alert_ok": False,
    "explicit_new_grad": False,
    "early_career_possible": False,
    "ranking_adjustment": 0,
    "source_url": "",
    "posting_status": "open",
    "profile": "new_grad",
}
_JOB_EMPTY_FIELDS = {
    "alternate_urls",
    "source_url_variants",
    "source_board_variants",
    "locations",
    "link_resolution",
    "posting_identity",
    "posting_family_id",
    "location_eligibility",
}

# Startup-stage evidence is a projection of the company-level research cache,
# which is loaded separately by the frontend.  Keeping the same long cited
# claim and explanation on every historical posting caused rescoring to add
# several megabytes without adding new evidence.  Persist the small stage and
# score fields; the UI and issue-delivery surfaces hydrate the cited claim
# from company_research.json.
_JOB_DERIVED_FIELDS = {
    "startup_stage_evidence",
    "startup_stage_reason",
}

# The full terminal record already lives in jobs_history.json. Keep just the
# fields needed to list and search closed postings in the primary web snapshot.
# Ranking and source metadata belong only in the full history record;
# duplicating them on every terminal summary exhausted the primary size limit.
# Keep score_version so saving a summary again remains compact and idempotent.
_JOB_TERMINAL_SUMMARY_FIELDS = {
    "id", "company", "title", "url", "locations", "first_seen", "last_seen_at",
    "posted_at", "posting_status", "posting_status_reason",
    "posting_status_changed_at", "closed_at", "last_closed_at",
    "manual_archived", "archived_at", "archived_by", "archive_reason", "score",
    "score_version", "lifecycle_events", _DETAIL_REF_FIELD,
}


def _compact_sponsorship_history(value: object) -> object:
    """Keep per-job DOL context small; coverage is shared by sponsorship.json."""
    if not isinstance(value, dict):
        return value
    compact = dict(value)
    compact.pop("coverage_quarters", None)
    # Missing counters are interpreted as zero by every consumer.  Do not
    # persist those repeated defaults on tens of thousands of rows.
    for key in ("certified_withdrawn_cases", "certified_withdrawn_workers"):
        if compact.get(key) == 0:
            compact.pop(key, None)
    return compact


def _compact_provenance_list(value: object, primary: object) -> object:
    """Dedupe provenance while keeping the primary value as the source of truth."""
    if not isinstance(value, list):
        return value
    result: list[object] = []
    for item in value:
        if not item or item == primary or item in result:
            continue
        result.append(item)
    return result


def _compact_score_dimensions(value: object) -> object:
    """Persist only non-zero dimensions; absent dimensions mean no signal."""
    if not isinstance(value, dict):
        return value
    return {key: amount for key, amount in value.items() if amount != 0}


def _prefix(namespace: str | None = None) -> str:
    """Keep the legacy new-grad filenames, prefixing only new lanes."""
    mode = namespace or profile_id()
    return "intern_" if mode == "internship" else ""


def _path(name: str, namespace: str | None = None, *, shared: bool = False) -> Path:
    return STATE_DIR / (name if shared else f"{_prefix(namespace)}{name}")


def _history_name(name: str) -> str | None:
    """Return the optional terminal-record shard for a job snapshot."""
    if name == "jobs.json":
        return "jobs_history.json"
    if name == "intern_jobs.json":
        return "intern_jobs_history.json"
    return None


def _details_dir_name(name: str) -> str | None:
    """Return the sibling directory containing bounded job detail shards."""
    if name == "jobs.json":
        return "jobs_details"
    if name == "intern_jobs.json":
        return "intern_jobs_details"
    return None


def _details_dir(path: Path) -> Path | None:
    name = _details_dir_name(path.name)
    return path.parent / name if name else None


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def _detail_signature(detail: dict) -> str:
    """Hash the exact payload so mixed primary/shard revisions are ignored."""
    return hashlib.sha256(_canonical_json(detail).encode("utf-8")).hexdigest()


def _primary_detail_hint(field: str, value: object) -> dict:
    keys = _PRIMARY_DETAIL_HINTS.get(field, ())
    if not isinstance(value, dict):
        return {}
    return {
        key: value[key]
        for key in keys
        if key in value and value[key] not in (None, [], "")
    }


def _hard_gate_reasons(value: object) -> list[str]:
    """Keep only UI-relevant rejection reasons in the compact job index."""
    if not isinstance(value, list):
        return []
    reasons: list[str] = []
    for item in value:
        reason = str(item)
        normalized = re.sub(r"^re-gate v\d+:\s*", "", reason, flags=re.IGNORECASE)
        if _HARD_GATE_REASON_RE.fullmatch(normalized) and reason not in reasons:
            reasons.append(reason)
    return reasons


def _detail_shard(name: str, key: str) -> str:
    directory = _details_dir_name(name)
    if not directory:
        raise ValueError(f"job detail sharding is unsupported for {name}")
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return f"{directory}/{digest[:2]}.json"


def _detail_ref(name: str, key: str, detail: dict) -> dict:
    return {
        "version": _DETAIL_SCHEMA_VERSION,
        "shard": _detail_shard(name, key),
        "key": key,
        "sha256": _detail_signature(detail),
    }


def _detail_path(primary_path: Path, ref: object) -> Path | None:
    """Resolve a pointer only inside the expected lane's detail directory."""
    directory = _details_dir(primary_path)
    if directory is None or not isinstance(ref, dict):
        return None
    if ref.get("version") != _DETAIL_SCHEMA_VERSION:
        return None
    shard = ref.get("shard")
    if not isinstance(shard, str) or not shard:
        return None
    key = ref.get("key")
    if not isinstance(key, str):
        return None
    shard_path = Path(shard)
    if shard_path.is_absolute():
        return None
    # Current pointers include the directory; accepting a bare filename keeps
    # local migration tools simple without permitting path traversal.
    candidate = (primary_path.parent / shard_path
                 if len(shard_path.parts) > 1
                 else directory / shard_path)
    if candidate.parent != directory or candidate.suffix != ".json":
        return None
    expected = Path(_detail_shard(primary_path.name, key))
    if shard_path not in {expected, Path(expected.name)}:
        return None
    return candidate


def _read_existing_detail_for_save(primary_path: Path, key: str, record: dict) -> tuple[dict, dict]:
    """Validate a compact pointer before any rebuilt fields can replace it."""
    ref = record.get(_DETAIL_REF_FIELD)
    detail_path = _detail_path(primary_path, ref)
    if (detail_path is None or not isinstance(ref, dict)
            or ref.get("key") != key):
        raise ValueError(
            f"cannot publish job snapshot: detail shard for {key} "
            "is missing or checksum mismatch"
        )
    shard = _load_path(detail_path, {})
    records = shard.get("records") if isinstance(shard, dict) else None
    signatures = shard.get("signatures") if isinstance(shard, dict) else None
    detail = records.get(key) if isinstance(records, dict) else None
    if (shard.get("schema_version") != _DETAIL_SCHEMA_VERSION
            or not isinstance(detail, dict)
            or not isinstance(signatures, dict)
            or signatures.get(key) != ref.get("sha256")
            or ref.get("sha256") != _detail_signature(detail)):
        raise ValueError(
            f"cannot publish job snapshot: detail shard for {key} "
            "is missing or checksum mismatch"
        )
    normalized = dict(ref)
    normalized["shard"] = _detail_shard(primary_path.name, key)
    return normalized, detail


def _load_path(p: Path, default):
    if not p.exists():
        return default
    with open(p) as f:
        return json.load(f)


def load(name: str, default, namespace: str | None = None):
    """Load a state file, merging the optional job-history shard on read."""
    return load_job_snapshot(_path(name, namespace), default)


def load_job_snapshot(path: Path, default):
    """Read a snapshot at an explicit path, restoring matching terminal records.

    Importers and local tools can read another checkout without changing the
    process-wide STATE_DIR or accidentally bypassing history hydration.
    """
    value = _load_path(path, default)
    history_name = _history_name(path.name)
    if not history_name or not isinstance(value, dict):
        return value
    value = _hydrate_job_details(path, value)
    history = _load_path(path.with_name(history_name), {})
    if not isinstance(history, dict) or not history:
        return value
    merged = dict(value)
    for key, full in history.items():
        summary = value.get(key)
        if not _is_terminal_job(summary) or not isinstance(full, dict):
            continue
        if (summary.get("posting_status") != full.get("posting_status")
                or summary.get("posting_status_changed_at") != full.get("posting_status_changed_at")):
            continue
        merged[key] = {**full, **summary}
    return merged


def _hydrate_job_details(path: Path, value: dict) -> dict:
    """Hydrate score detail shards while tolerating a mixed or old checkout."""
    refs: dict[str, tuple[dict, Path]] = {}
    for key, record in value.items():
        if not isinstance(record, dict):
            continue
        ref = record.get(_DETAIL_REF_FIELD)
        detail_path = _detail_path(path, ref)
        if (detail_path is not None and isinstance(ref, dict)
                and ref.get("key") == str(key)):
            refs[str(key)] = (ref, detail_path)
    if not refs:
        return value

    loaded: dict[Path, dict] = {}
    result = dict(value)
    for key, (ref, detail_path) in refs.items():
        if detail_path not in loaded:
            shard = _load_path(detail_path, {})
            loaded[detail_path] = shard if isinstance(shard, dict) else {}
        shard = loaded[detail_path]
        if shard.get("schema_version") != _DETAIL_SCHEMA_VERSION:
            continue
        records = shard.get("records")
        detail = records.get(ref["key"]) if isinstance(records, dict) else None
        signatures = shard.get("signatures")
        if (not isinstance(detail, dict)
                or not isinstance(signatures, dict)
                or signatures.get(ref["key"]) != ref.get("sha256")
                or ref.get("sha256") != _detail_signature(detail)):
            # Never hydrate a record from a different revision's shard.  Keep
            # the pointer visible to repair tooling instead.
            continue
        hydrated = dict(result[key])
        hydrated.pop(_DETAIL_REF_FIELD, None)
        hydrated.update(detail)
        result[key] = hydrated
    return result


def _compact_job_record(record: object) -> object:
    """Return a sparse, lossless-on-read copy of one generated job record."""
    if not isinstance(record, dict):
        return record
    # Only fully scored crawler output is guaranteed to have every omitted
    # default reconstructible. Keep hand-authored/legacy rows byte-for-byte
    # compatible with callers that still inspect optional keys directly.
    if not record.get("score_version") or record.get("manual_added"):
        return record
    compact = dict(record)
    if compact.get("score_dimensions_raw") == compact.get("score_dimensions"):
        compact.pop("score_dimensions_raw", None)
    for key, default in _JOB_DEFAULTS.items():
        if compact.get(key) == default:
            compact.pop(key, None)
    for key in _JOB_EMPTY_FIELDS:
        if compact.get(key) in (None, "", [], {}):
            compact.pop(key, None)
    if "score_dimensions" in compact:
        compact["score_dimensions"] = _compact_score_dimensions(
            compact["score_dimensions"])
    for key, primary_key in (("source_variants", "source"),
                             ("source_board_variants", "source_board"),
                             ("source_url_variants", "source_url")):
        if key in compact:
            compact[key] = _compact_provenance_list(compact[key], compact.get(primary_key))
            if not compact[key]:
                compact.pop(key, None)
    for key in _JOB_DERIVED_FIELDS:
        compact.pop(key, None)
    if "sponsorship_history" in compact:
        compact["sponsorship_history"] = _compact_sponsorship_history(
            compact["sponsorship_history"])
    return compact


def _is_terminal_job(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    status = str(record.get("posting_status", "")).strip().lower()
    return status in {"expired", "filled", "archived"} or bool(record.get("closed_at"))


def _detail_candidate(value: object) -> dict | None:
    """Select generated detail fields for one bounded, versioned shard row.

    Open rows move the large score ledger, drawer-only eligibility detail, and
    score dimensions. Compact filter and ranking fields remain in the index.
    Terminal rows already expose a deliberately small summary in the index,
    so their remaining compacted fields move together and the old history file
    can shrink on the next write. Both shapes use the same pointer and checksum
    contract.
    """
    if not isinstance(value, dict) or not value.get("score_version") or value.get("manual_added"):
        return None
    compact = _compact_job_record(value)
    if _is_terminal_job(value) and isinstance(compact, dict):
        detail = {
            key: item for key, item in compact.items()
            if key not in _JOB_TERMINAL_SUMMARY_FIELDS and key != _DETAIL_REF_FIELD
        }
        # Keep the complete lifecycle ledger in the detail shard even though
        # the primary terminal summary exposes only the latest four entries.
        if isinstance(compact.get("lifecycle_events"), list):
            detail["lifecycle_events"] = compact["lifecycle_events"]
        return detail or None
    detail = {}
    # A complete crawler row has a score ledger even when every dimension is
    # zero. Keep that explicit empty ledger for compatibility with the score
    # health/readback contract; sparse merge fixtures deliberately omit the
    # company/title pair and remain byte-for-byte sparse.
    if (isinstance(compact, dict)
            and "score_dimensions" not in compact
            and compact.get("company") and compact.get("title")):
        compact["score_dimensions"] = {}
    for field in _DETAIL_FIELDS:
        if field not in compact:
            continue
        if field == "eligibility_evidence" and not compact[field]:
            continue
        detail[field] = compact[field]
    return detail or None


def _prepared(name: str, obj: object, detail_refs: dict[str, dict] | None = None) -> object:
    if name not in {"jobs.json", "intern_jobs.json"} or not isinstance(obj, dict):
        return obj
    detail_refs = detail_refs or {}
    prepared = {}
    for key, value in obj.items():
        compact = _compact_job_record(value)
        key_text = str(key)
        ref = detail_refs.get(key_text)
        if isinstance(compact, dict) and ref is not None:
            compact = dict(compact)
            for field in _DETAIL_FIELDS:
                compact.pop(field, None)
            if isinstance(value, dict):
                for field in _PRIMARY_DETAIL_HINTS:
                    if isinstance(value.get(field), dict):
                        hint = _primary_detail_hint(field, value[field])
                        if hint or field == "posting":
                            compact[field] = hint
                if _is_terminal_job(value) and "posting_status_reason" in value:
                    compact["posting_status_reason"] = value["posting_status_reason"]
                if "score_reasons" in value:
                    compact.pop("gating_summary", None)
                    hard_reasons = _hard_gate_reasons(value.get("score_reasons"))
                    if hard_reasons:
                        compact["gating_summary"] = {"reasons": hard_reasons}
            if (_is_terminal_job(value)
                    and isinstance(value.get("lifecycle_events"), list)):
                compact["lifecycle_events"] = value["lifecycle_events"][-4:]
            compact[_DETAIL_REF_FIELD] = ref
        # Closed postings retain a web-facing summary in the primary snapshot;
        # the remaining compacted record lives in the detail shard. A legacy
        # history file remains readable for old checkouts.
        if (_is_terminal_job(value) and isinstance(value, dict)
                and value.get("score_version") and not value.get("manual_added")
                and isinstance(compact, dict)):
            if isinstance(compact.get("lifecycle_events"), list):
                compact["lifecycle_events"] = compact["lifecycle_events"][-4:]
            compact = {field: item for field, item in compact.items()
                       if field in _JOB_TERMINAL_SUMMARY_FIELDS}
        prepared[key] = compact
    return prepared


def _prepared_history(
    obj: object,
    detail_keys: set[str] | None = None,
) -> dict:
    """Build the legacy fallback ledger for terminal records without shards."""
    if not isinstance(obj, dict):
        return {}
    detail_keys = detail_keys or set()
    prepared = {}
    for key, value in obj.items():
        if not _is_terminal_job(value):
            continue
        compact = _compact_job_record(value)
        if str(key) in detail_keys:
            # New terminal rows are fully recoverable from jobs_details. Keep
            # the legacy history reader for old checkouts, but do not duplicate
            # the same large record in every future history snapshot.
            continue
        prepared[key] = compact
    return prepared


def _max_job_snapshot_bytes() -> int:
    value = os.getenv("RADAR_MAX_JOB_SNAPSHOT_BYTES", "").strip()
    if not value:
        return DEFAULT_MAX_JOB_SNAPSHOT_BYTES
    try:
        return max(1, int(value))
    except ValueError as exc:
        raise ValueError("RADAR_MAX_JOB_SNAPSHOT_BYTES must be an integer") from exc


def _max_job_detail_shard_bytes() -> int:
    value = os.getenv("RADAR_MAX_JOB_DETAIL_SHARD_BYTES", "").strip()
    if not value:
        return DEFAULT_MAX_JOB_DETAIL_SHARD_BYTES
    try:
        return max(1, int(value))
    except ValueError as exc:
        raise ValueError("RADAR_MAX_JOB_DETAIL_SHARD_BYTES must be an integer") from exc


def _stage_json(path: Path, name: str, obj: object) -> Path:
    """Serialize one file beside its destination and return the temp path."""
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with open(tmp, "w") as f:
            json.dump(obj, f, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))
            f.write("\n")
        size_limit = None
        if name in {"jobs.json", "intern_jobs.json"}:
            size_limit = _max_job_snapshot_bytes()
        elif path.parent.name in {"jobs_details", "intern_jobs_details"}:
            size_limit = _max_job_detail_shard_bytes()
        if size_limit is not None and tmp.stat().st_size > size_limit:
            size = tmp.stat().st_size
            if path.parent.name in {"jobs_details", "intern_jobs_details"}:
                raise ValueError(
                    f"generated job detail shard is {size:,} bytes; limit is "
                    f"{size_limit:,}. Split the deterministic detail shards before publishing"
                )
            raise ValueError(
                f"generated job snapshot is {size:,} bytes; limit is "
                f"{size_limit:,}. Compact or shard state before publishing"
            )
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return tmp


def _write_many(entries: list[tuple[Path, str, object]]) -> None:
    """Stage all files before replacing any destination."""
    staged: list[tuple[Path, Path]] = []
    try:
        for path, name, obj in entries:
            staged.append((_stage_json(path, name, obj), path))
        for tmp, path in staged:
            tmp.replace(path)
    except Exception:
        for tmp, _ in staged:
            tmp.unlink(missing_ok=True)
        raise


def save(name: str, obj, namespace: str | None = None) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    primary_path = _path(name, namespace)
    detail_refs: dict[str, dict] = {}
    detail_records: dict[str, dict[str, dict]] = {}
    detail_record_keys: set[str] = set()
    details_path = _details_dir(primary_path)
    if details_path is not None and isinstance(obj, dict):
        # Build new details from the full in-memory records.  If a caller is
        # merging a compact raw snapshot, retain records referenced by the
        # existing pointer instead of silently deleting their evidence.
        for key, value in obj.items():
            key_text = str(key)
            existing_ref = None
            existing_detail = None
            if isinstance(value, dict) and _DETAIL_REF_FIELD in value:
                existing_ref, existing_detail = _read_existing_detail_for_save(
                    primary_path, key_text, value)
            detail = _detail_candidate(value)
            if detail is not None:
                if existing_detail is not None:
                    # A compact writer may have rebuilt only score fields. Do
                    # not discard other verified detail from its valid shard.
                    for field in _PRIMARY_DETAIL_HINTS:
                        detail.pop(field, None)
                    detail = {**existing_detail, **detail}
                ref = _detail_ref(primary_path.name, key_text, detail)
                detail_refs[key_text] = ref
                detail_records.setdefault(ref["shard"], {})[key_text] = detail
                detail_record_keys.add(key_text)
                continue
            if existing_ref is None:
                continue
            detail_refs[key_text] = existing_ref
            detail_records.setdefault(existing_ref["shard"], {})[key_text] = existing_detail
            detail_record_keys.add(key_text)

    prepared = _prepared(name, obj, detail_refs)
    entries = [(primary_path, name, prepared)]
    history_name = _history_name(name)
    if history_name:
        history_path = _path(history_name, namespace)
        history = _prepared_history(obj, detail_record_keys)
        # Clear a stale shard when a later snapshot has no terminal rows, but
        # avoid creating an empty generated file on a fresh crawl.
        if history or history_path.exists():
            entries.append((history_path, history_name, history))
    active_shards = set(detail_records)
    if details_path is not None and detail_records:
        details_path.mkdir(parents=True, exist_ok=True)
        for shard_name in sorted(detail_records):
            shard_path = primary_path.parent / shard_name
            records = detail_records[shard_name]
            entries.append((shard_path, shard_path.name, {
                "schema_version": _DETAIL_SCHEMA_VERSION,
                "records": records,
                "signatures": {
                    key: _detail_signature(detail)
                    for key, detail in sorted(records.items())
                },
            }))
    _write_many(entries)
    if details_path is not None and details_path.exists():
        # Unreferenced shards are safe to remove after all new files have been
        # staged and replaced.  A crash before this cleanup leaves harmless
        # unreachable files; pointers and signatures remain authoritative.
        for stale in details_path.glob("*.json"):
            if f"{details_path.name}/{stale.name}" not in active_shards:
                stale.unlink(missing_ok=True)


def load_shared(name: str, default):
    p = _path(name, shared=True)
    if not p.exists():
        return default
    with open(p) as f:
        return json.load(f)


def save_shared(name: str, obj) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    p = _path(name, shared=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f, sort_keys=True, ensure_ascii=False,
                  separators=(",", ":"))
        f.write("\n")
    tmp.replace(p)


def jobs() -> dict:
    return load("jobs.json", {})


def companies() -> dict:
    return load("companies.json", {})


def feedback() -> dict:
    return load("feedback.json", {"company_boosts": {}, "token_boosts": {},
                                    "explicit_company_boosts": {},
                                    "explicit_token_boosts": {},
                                    "negative_companies": [], "taste_events": []})


def score_preferences() -> dict:
    return load("score_preferences.json", {
        "version": 1,
        "enabled_dimensions": {
            "base": True, "role_fit": True, "eligibility": True,
            "mission": True, "company_quality": True, "compensation": True,
            "personal_signal": True, "timing_access": True,
        },
    })


def applied() -> list:
    return load("applied.json", [])


def shortlist() -> list:
    """Jobs the user checked as 'save for later' — not confirmed applications.
    Promoted to applied.json by email_watch.py when a confirmation email is
    matched, or manually via the `applied <url>` comment command."""
    return load("shortlist.json", [])
