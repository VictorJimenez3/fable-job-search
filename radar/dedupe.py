"""Conservative duplicate repair for the durable jobs state.

Exact canonical posting URLs are safe identity boundaries: the same URL can
be emitted with different titles or locations by an aggregator and its ATS.
The merge keeps the stronger record's identity, carries over useful evidence,
and returns aliases so application, issue, and private workspace references
remain usable.
"""
from __future__ import annotations

from .identity import canonical_url
from .link_resolver import is_aggregator_url
from .models import norm


_AGGREGATOR_SOURCES = {
    "simplify", "vansh", "jobright", "jobright_pm", "speedyapply",
    "zapply", "zapply_pm", "hn", "simplify_internship",
    "speedyapply_internship", "zapply_internship", "dreamwork_internship",
}


def _record_url(record: dict) -> str:
    return canonical_url(record.get("url"))


def _winner_key(record: dict, key: str) -> tuple:
    source = str(record.get("source") or "").casefold()
    # Manual records carry owner intent; direct ATS records carry better
    # provenance than aggregator copies.  Active and evidence-rich records
    # should survive a cleanup over stale/closed twins.
    return (
        1 if record.get("manual_added") else 0,
        1 if record.get("ats") or source not in _AGGREGATOR_SOURCES else 0,
        1 if str(record.get("posting_status", "open")).casefold() == "open" else 0,
        1 if record.get("posting") else 0,
        1 if record.get("quality") else 0,
        int(record.get("last_seen_at") or 0),
        int(record.get("first_seen") or 0),
        int(record.get("score") or 0),
        key,
    )


def _append_unique(target: list, values: list) -> None:
    for value in values:
        if value not in target:
            target.append(value)


def _location_tokens(record: dict) -> set[str]:
    noise = {
        "remote", "united", "states", "state", "usa", "us", "america",
        "onsite", "on", "site", "hybrid", "virtual", "area", "the",
    }
    state_codes = {
        "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga",
        "hi", "id", "il", "in", "ia", "ks", "ky", "la", "me", "md",
        "ma", "mi", "mn", "ms", "mo", "mt", "ne", "nv", "nh", "nj",
        "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa", "ri", "sc",
        "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy",
        "dc",
    }
    state_names = {
        "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
        "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
        "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
        "maine", "maryland", "massachusetts", "michigan", "minnesota",
        "mississippi", "missouri", "montana", "nebraska", "nevada",
        "hampshire", "jersey", "mexico", "york", "carolina", "dakota",
        "ohio", "oklahoma", "oregon", "pennsylvania", "rhode", "tennessee",
        "texas", "utah", "vermont", "virginia", "washington", "wisconsin",
        "wyoming",
    }
    tokens = set()
    for location in record.get("locations") or []:
        tokens.update(token for token in norm(location).split() if token not in noise)
    meaningful = tokens - state_codes - state_names
    # State-only/country-only locations remain useful as unknown rather than
    # creating false same-state matches between distinct city postings.
    return meaningful or tokens


def _is_aggregator_record(record: dict) -> bool:
    source = str(record.get("source") or "").casefold()
    if source not in _AGGREGATOR_SOURCES:
        return False
    # A Jobright record may already have been promoted to a direct ATS URL.
    # Its remaining source label is useful provenance, but it is no longer a
    # safe aggregator-only identity for another cross-source merge.
    return is_aggregator_url(record.get("url")) or not record.get("ats")


def _same_location_or_unknown(left: dict, right: dict) -> bool:
    left_tokens = _location_tokens(left)
    right_tokens = _location_tokens(right)
    if not left_tokens or not right_tokens:
        return True
    return bool(left_tokens & right_tokens)


def _merge_record(winner: dict, loser: dict) -> dict:
    """Merge non-conflicting evidence without replacing owner state."""
    merged = dict(winner)
    for key, value in loser.items():
        if key in {"id", "url", "company", "title", "source", "ats"}:
            continue
        if key not in merged or merged[key] in (None, "", [], {}):
            merged[key] = value

    locations = list(merged.get("locations") or [])
    _append_unique(locations, list(loser.get("locations") or []))
    if locations:
        merged["locations"] = locations

    sources = list(merged.get("source_variants") or [])
    _append_unique(sources, [winner.get("source"), loser.get("source")])
    merged["source_variants"] = [s for s in sources if s]

    source_urls = list(merged.get("source_url_variants") or [])
    _append_unique(source_urls, [winner.get("source_url"), loser.get("source_url")])
    _append_unique(source_urls, list(loser.get("source_url_variants") or []))
    merged["source_url_variants"] = [u for u in source_urls if u]

    alternate_urls = list(merged.get("alternate_urls") or [])
    winner_url = canonical_url(winner.get("url"))
    for url in [loser.get("url"), *(loser.get("alternate_urls") or [])]:
        if url and canonical_url(url) != winner_url:
            _append_unique(alternate_urls, [url])
    merged["alternate_urls"] = alternate_urls

    if (not merged.get("link_resolution") or
            merged.get("link_resolution", {}).get("status") not in {"resolved"}):
        if loser.get("link_resolution"):
            merged["link_resolution"] = loser["link_resolution"]

    for field in ("first_seen",):
        values = [v for v in (merged.get(field), loser.get(field)) if v]
        if values:
            merged[field] = min(values)
    for field in ("last_seen_at", "last_closed_at", "closed_at"):
        values = [v for v in (merged.get(field), loser.get(field)) if v]
        if values:
            merged[field] = max(values)

    if (loser.get("score") or 0) > (merged.get("score") or 0):
        merged["score"] = loser["score"]
    if loser.get("alert_ok"):
        merged["alert_ok"] = True

    if isinstance(loser.get("posting"), dict):
        posting = dict(loser["posting"])
        posting.update(merged.get("posting") or {})
        merged["posting"] = posting
    if isinstance(loser.get("lifecycle_events"), list):
        events = list(merged.get("lifecycle_events") or [])
        for event in loser["lifecycle_events"]:
            if event not in events:
                events.append(event)
        merged["lifecycle_events"] = events
    return merged


def collapse_jobs(jobs: dict) -> tuple[dict, dict, int]:
    """Collapse exact canonical-URL duplicates.

    Returns ``(collapsed_jobs, aliases, merged_count)`` where aliases maps an
    old job ID to the surviving ID. Records without a URL are left untouched.
    """
    groups: dict[str, list[tuple[str, dict]]] = {}
    for key, record in jobs.items():
        url = _record_url(record)
        if url:
            groups.setdefault(url, []).append((str(key), record))

    aliases: dict[str, str] = {}
    collapsed = dict(jobs)
    merged_count = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        winner_key, winner = max(members, key=lambda pair: _winner_key(pair[1], pair[0]))
        merged = dict(winner)
        for key, record in members:
            if key == winner_key:
                continue
            merged = _merge_record(merged, record)
            aliases[key] = winner_key
            collapsed.pop(key, None)
            merged_count += 1
        merged["id"] = winner.get("id") or winner_key
        collapsed[winner_key] = merged

    return collapsed, aliases, merged_count


def collapse_cross_source_jobs(jobs: dict) -> tuple[dict, dict, int]:
    """Merge only exact company/title aggregator copies with direct ATS rows.

    URL equality remains the strongest identity boundary.  This second pass is
    intentionally narrower than fuzzy matching: a source-only row is merged
    when its normalized company/title has one compatible direct candidate, or
    when a multi-candidate company/title group has exactly one location match.
    Ambiguous roles stay separate so breadth is never traded for a guessed
    identity.
    """
    direct_by_key: dict[tuple[str, str], list[tuple[str, dict]]] = {}
    aggregator_rows: list[tuple[str, dict]] = []
    for key, record in jobs.items():
        identity = (norm(record.get("company")), norm(record.get("title")))
        if not identity[0] or not identity[1]:
            continue
        if _is_aggregator_record(record):
            aggregator_rows.append((str(key), record))
        elif record.get("ats"):
            direct_by_key.setdefault(identity, []).append((str(key), record))

    collapsed = dict(jobs)
    aliases: dict[str, str] = {}
    merged_count = 0
    for aggregator_id, aggregator in aggregator_rows:
        if aggregator_id not in collapsed:
            continue
        candidates = direct_by_key.get(
            (norm(aggregator.get("company")), norm(aggregator.get("title"))), [])
        if len(candidates) == 1:
            candidate_id, direct = candidates[0]
            winner_id = candidate_id if _same_location_or_unknown(aggregator, direct) else None
        else:
            matching = [
                (candidate_id, direct) for candidate_id, direct in candidates
                if _same_location_or_unknown(aggregator, direct)
            ]
            winner_id, direct = matching[0] if len(matching) == 1 else (None, None)
        if not winner_id or winner_id not in collapsed:
            continue
        collapsed[winner_id] = _merge_record(collapsed[winner_id], aggregator)
        collapsed.pop(aggregator_id, None)
        aliases[aggregator_id] = winner_id
        merged_count += 1

    return collapsed, aliases, merged_count


def resolve_alias(job_id: str, aliases: dict[str, str]) -> str:
    """Follow a persisted alias chain safely."""
    current = str(job_id)
    visited = set()
    while current in aliases and current not in visited:
        visited.add(current)
        current = str(aliases[current])
    return current


def remap_entry_ids(entries: list[dict], aliases: dict[str, str]) -> int:
    changed = 0
    for entry in entries:
        old = str(entry.get("id", ""))
        new = resolve_alias(old, aliases)
        if old and new != old:
            entry["id"] = new
            entry["job_id_alias"] = old
            changed += 1
    return changed


def remap_web_jobs(web: dict, aliases: dict[str, str]) -> int:
    """Move private workspace keys while preserving user notes and links."""
    jobs = web.get("jobs") or {}
    remapped: dict[str, dict] = {}
    changed = 0
    for old, value in jobs.items():
        new = resolve_alias(str(old), aliases)
        if new != old:
            changed += 1
        current = remapped.setdefault(new, {})
        if isinstance(value, dict):
            for key, item in value.items():
                if key not in current or current[key] in (None, "", [], {}):
                    current[key] = item
    if changed:
        web["jobs"] = remapped
    return changed
