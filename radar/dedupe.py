"""Conservative duplicate repair for the durable jobs state.

Exact canonical posting URLs are safe identity boundaries: the same URL can
be emitted with different titles or locations by an aggregator and its ATS.
The merge keeps the stronger record's identity, carries over useful evidence,
and returns aliases so application, issue, and private workspace references
remain usable.  A second, narrower family pass handles providers that mint a
different URL for every location of one employer requisition.  It requires
shared title-core evidence plus direct/aggregator or same-board evidence; a
same-title collision by itself is never enough.
"""
from __future__ import annotations

from . import lifecycle
from .identity import canonical_url, posting_key
from .link_resolver import is_aggregator_url
from .models import norm

_AGGREGATOR_SOURCES = {
    "simplify", "vansh", "jobright", "jobright_pm", "speedyapply",
    "zapply", "zapply_pm", "hn", "simplify_internship",
    "speedyapply_internship", "zapply_internship", "dreamwork_internship",
}

# These words describe the hiring program or level rather than the work.  They
# may be removed while comparing an official title with an aggregator's title,
# but year tokens remain meaningful so 2026 and 2027 postings cannot collapse.
_TITLE_VARIANT_MARKERS = {
    "new", "newgrad", "grad", "graduate", "early", "career", "campus",
    "college", "university", "entry", "level", "intern", "internship",
    "co", "op",
}

_LOCATION_ALIASES = {
    "nyc": "new york",
    "la": "los angeles",
    "sf": "san francisco",
    "dc": "washington",
}


def _record_url(record: dict) -> str:
    resolution = record.get("link_resolution") or {}
    resolved = resolution.get("resolved_url") if resolution.get("status") == "resolved" else ""
    if resolved and not is_aggregator_url(resolved):
        return posting_key(resolved)
    return posting_key(record.get("url"))


def _record_source_url(record: dict) -> str:
    return canonical_url(
        record.get("source_url") or (record.get("source_url_variants") or [""])[0]
    )


def _title_words(record: dict) -> set[str]:
    return set(norm(record.get("title")).split())


def title_family(record: dict) -> str:
    """Return the work-oriented title core used for variant comparison."""
    words = [word for word in norm(record.get("title")).split()
             if word not in _TITLE_VARIANT_MARKERS]
    return " ".join(words)


def _has_title_variant_marker(record: dict) -> bool:
    return bool(_title_words(record) & _TITLE_VARIANT_MARKERS)


def _is_direct_record(record: dict) -> bool:
    """Whether the primary URL is an employer/ATS URL, even if a feed found it."""
    return bool(_record_url(record)) and not _is_aggregator_record(record)


def _posting_variant_reason(left: dict, right: dict) -> str:
    """Explain why two records are safe candidates for one posting family."""
    if norm(left.get("company")) != norm(right.get("company")):
        return ""
    left_title = norm(left.get("title"))
    right_title = norm(right.get("title"))
    if not left_title or not right_title:
        return ""
    if left_title == right_title:
        title_reason = "same employer and title"
    elif (title_family(left) and title_family(left) == title_family(right)
          and (_has_title_variant_marker(left) or _has_title_variant_marker(right))):
        title_reason = "same employer and title core with a hiring-program marker"
    else:
        return ""
    if not _same_location_or_unknown(left, right):
        return ""
    return f"{title_reason}; compatible location evidence"


def _winner_key(record: dict, key: str) -> tuple:
    # Manual records carry owner intent; direct ATS records carry better
    # provenance than aggregator copies.  Active and evidence-rich records
    # should survive a cleanup over stale/closed twins.
    return (
        1 if record.get("manual_added") else 0,
        1 if _is_direct_record(record) else 0,
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
        text = norm(location).replace("new york city", "new york")
        for token in text.split():
            alias = _LOCATION_ALIASES.get(token, token)
            tokens.update(word for word in alias.split() if word not in noise)
    meaningful = tokens - state_codes - state_names
    # State-only/country-only locations are unknown rather than evidence for a
    # same-state match between distinct city postings.
    return meaningful


def _is_aggregator_record(record: dict) -> bool:
    # A record found in Simplify/Vansh/etc. may already be an official employer
    # URL.  The URL, not the discovery label, decides whether it is still an
    # aggregator-only identity.  This is important for Google: Simplify can
    # surface the official multi-location row while Jobright emits one URL per
    # city.
    return is_aggregator_url(record.get("url"))


def _same_location_or_unknown(left: dict, right: dict) -> bool:
    left_tokens = _location_tokens(left)
    right_tokens = _location_tokens(right)
    if not left_tokens or not right_tokens:
        return True
    return bool(left_tokens & right_tokens)


def _merge_record(winner: dict, loser: dict, reason: str = "") -> dict:
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

    source_boards = list(merged.get("source_board_variants") or [])
    _append_unique(source_boards, [winner.get("source_board"), loser.get("source_board")])
    _append_unique(source_boards, list(loser.get("source_board_variants") or []))
    merged["source_board_variants"] = [board for board in source_boards if board]

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
    for field in ("last_seen_at",):
        values = [v for v in (merged.get(field), loser.get(field)) if v]
        if values:
            merged[field] = max(values)

    # Lifecycle state is evidence, not an ordinary winner field. Prefer the
    # freshest verified employer status, while retaining every closure event
    # from the losing source for auditability. ``last_verified_open_at`` was
    # added after the first lifecycle schema, so use ``get`` for old rows.
    def lifecycle_authority_key(record: dict) -> tuple:
        terminal = lifecycle.is_terminal(record)
        quality = record.get("quality") or {}
        direct = not is_aggregator_url(record.get("url"))
        if terminal:
            quality_closed = quality.get("live") is False
            if quality_closed:
                checked = (record.get("lifecycle_checked_at") or
                           quality.get("checked_at") or 0)
            elif direct:
                checked = (record.get("lifecycle_checked_at") or
                           record.get("closed_at") or
                           record.get("posting_status_changed_at") or 0)
            else:
                # A feed/aggregator closure is useful history, but it is not
                # proof that the employer's still-live application is closed.
                checked = 0
            # Explicit quality liveness is strong negative evidence. A bare
            # status copied from an aggregator is weaker and must not defeat a
            # direct official row merely because it has ``closed_at``.
            evidenced = quality_closed
        else:
            checked = (record.get("last_verified_open_at") or
                       (quality.get("checked_at") if quality.get("live") is True else 0)
                       or 0)
            evidenced = bool(record.get("last_verified_open_at") or quality.get("live") is True)
        return (
            bool(record.get("manual_archived")),
            evidenced,
            int(checked),
            direct,
            terminal,
        )

    authority = max((winner, loser), key=lifecycle_authority_key)
    lifecycle_fields = tuple(dict.fromkeys(
        (*lifecycle.LIFECYCLE_FIELDS, "last_verified_open_at")))
    for field in lifecycle_fields:
        if field in {"lifecycle_events", "last_seen_at"}:
            continue
        if field in authority:
            merged[field] = authority[field]
        else:
            merged.pop(field, None)
    merged["posting_status"] = lifecycle.status_of(authority)
    if authority.get("quality"):
        merged["quality"] = dict(authority["quality"])
    else:
        merged.pop("quality", None)
    if lifecycle.is_terminal(merged):
        merged["alert_ok"] = False

    # A resolved aggregator row is useful provenance, not an application
    # target. Promote the verified employer URL while retaining the original
    # source link as an alternate.
    if is_aggregator_url(merged.get("url")):
        official = next((record for record in (winner, loser)
                         if _is_direct_record(record)), None)
        resolution = merged.get("link_resolution") or {}
        direct_url = official.get("url") if official else resolution.get("resolved_url")
        if direct_url and not is_aggregator_url(direct_url):
            _append_unique(merged["alternate_urls"], [merged["url"]])
            merged["url"] = direct_url
            if official:
                merged["ats"] = official.get("ats", "")
    if (not is_aggregator_url(merged.get("url"))
            and (merged.get("link_resolution") or {}).get("status") == "closed"):
        merged["link_resolution"] = {
            **merged["link_resolution"], "status": "resolved",
            "source_status": "closed", "resolved_url": merged["url"],
        }

    if (loser.get("score") or 0) > (merged.get("score") or 0):
        merged["score"] = loser["score"]
    # Eligibility is a negative-safe verdict, not additive provenance. Keep
    # the winner's explicit suppression instead of allowing an older/open
    # duplicate to re-enable delivery; only backfill when the winner omitted
    # the field entirely.
    if "alert_ok" in winner:
        merged["alert_ok"] = bool(winner.get("alert_ok"))
    elif "alert_ok" in loser:
        merged["alert_ok"] = bool(loser.get("alert_ok"))

    if isinstance(loser.get("posting"), dict):
        posting = dict(loser["posting"])
        posting.update(merged.get("posting") or {})
        merged["posting"] = posting
    events = []
    for record in (winner, loser):
        source_url = record.get("url", "")
        for event in record.get("lifecycle_events") or []:
            event = {**event, "source_url": event.get("source_url") or source_url}
            if event not in events:
                events.append(event)
        closed = int(record.get("closed_at") or record.get("posting_status_changed_at")
                     or (record.get("quality") or {}).get("checked_at") or 0)
        status = lifecycle.status_of(record)
        if lifecycle.is_terminal(record) and not any(
                event.get("status") == status and event.get("at") == closed
                and event.get("source_url") == source_url for event in events):
            events.append({"status": status, "at": closed, "source_url": source_url,
                           "reason": record.get("posting_status_reason")
                           or (record.get("quality") or {}).get("reason")
                           or "recorded closure from merged source"})
    if events:
        merged["lifecycle_events"] = sorted(events, key=lambda event: int(event.get("at") or 0))

    # A fresh open sighting can contribute evidence, but never re-enable
    # delivery after the merged record has authoritative terminal evidence.
    if lifecycle.is_terminal(merged):
        merged["alert_ok"] = False

    winner_identity = merged.get("posting_identity") or {}
    loser_identity = loser.get("posting_identity") or {}
    winner_count = int(winner_identity.get("variant_count") or 1)
    loser_count = int(loser_identity.get("variant_count") or 1)
    reasons = list(winner_identity.get("matched_by") or [])
    _append_unique(reasons, list(loser_identity.get("matched_by") or []))
    if reason:
        _append_unique(reasons, [reason])
    if reasons or winner_count > 1 or loser_count > 1:
        merged["posting_identity"] = {
            "variant_count": winner_count + loser_count,
            "matched_by": reasons[:6],
        }
    return merged


def collapse_jobs(jobs: dict) -> tuple[dict, dict, int]:
    """Collapse exact posting-identity duplicates.

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
            merged = _merge_record(merged, record, "same canonical posting identity")
            aliases[key] = winner_key
            collapsed.pop(key, None)
            merged_count += 1
        merged["id"] = winner.get("id") or winner_key
        merged["posting_family_id"] = winner_key
        collapsed[winner_key] = merged

    return collapsed, aliases, merged_count


def collapse_cross_source_jobs(jobs: dict) -> tuple[dict, dict, int]:
    """Merge narrow company/title-family aggregator copies with direct rows.

    URL equality remains the strongest identity boundary.  This second pass is
    intentionally narrower than fuzzy matching: a source-only row is merged
    when its normalized company and work-oriented title family have one
    compatible direct candidate, or when a multi-candidate family has exactly
    one location match. Ambiguous roles stay separate so breadth is never
    traded for a guessed identity.
    """
    direct_by_key: dict[tuple[str, str], list[tuple[str, dict]]] = {}
    aggregator_rows: list[tuple[str, dict]] = []
    for key, record in jobs.items():
        identity = (norm(record.get("company")), norm(record.get("title")))
        if not identity[0] or not identity[1]:
            continue
        if _is_aggregator_record(record):
            aggregator_rows.append((str(key), record))
        elif _record_url(record):
            family_identity = (identity[0], title_family(record))
            direct_by_key.setdefault(family_identity, []).append((str(key), record))

    collapsed = dict(jobs)
    aliases: dict[str, str] = {}
    merged_count = 0
    for aggregator_id, aggregator in aggregator_rows:
        if aggregator_id not in collapsed:
            continue
        candidates = direct_by_key.get(
            (norm(aggregator.get("company")), title_family(aggregator)), [])
        matching = [
            (candidate_id, direct, _posting_variant_reason(aggregator, direct))
            for candidate_id, direct in candidates
            if (_posting_variant_reason(aggregator, direct)
                    and _record_url(aggregator) == _record_url(direct))
        ]
        winner_id, direct, reason = matching[0] if len(matching) == 1 else (None, None, "")
        if not winner_id or winner_id not in collapsed:
            continue
        collapsed[winner_id] = _merge_record(collapsed[winner_id], aggregator, reason)
        collapsed[winner_id]["posting_family_id"] = winner_id
        collapsed.pop(aggregator_id, None)
        aliases[aggregator_id] = winner_id
        merged_count += 1

    return collapsed, aliases, merged_count


def collapse_location_variants(jobs: dict) -> tuple[dict, dict, int]:
    """Collapse same-board aggregator rows that represent one multi-location role.

    This pass intentionally excludes employer/ATS records.  It only runs when
    the same aggregator board supplied the same employer/title family, the
    title contains an early-career/program marker, and a source URL identifies
    the shared board.  That keeps ordinary same-title requisitions (for
    example, dozens of local sales jobs) as separate application targets.
    """
    groups: dict[tuple[str, str, str, str], list[tuple[str, dict]]] = {}
    for key, record in jobs.items():
        if not _is_aggregator_record(record):
            continue
        source_url = _record_source_url(record)
        company = norm(record.get("company"))
        family = title_family(record)
        if not source_url or not company or not family or not _has_title_variant_marker(record):
            continue
        groups.setdefault((str(record.get("source") or "").casefold(), source_url,
                          company, family), []).append((str(key), record))

    collapsed = dict(jobs)
    aliases: dict[str, str] = {}
    merged_count = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        # Same-board/title/day is not enough to prove that separate provider
        # URLs are one requisition. Require a verified common destination.
        if len({_record_url(record) for _, record in members}) != 1:
            continue
        posted_days = {
            int(record["posted_at"]) // 86400
            for _, record in members if record.get("posted_at")
        }
        # A location fan-out is normally emitted with one common posting date.
        # A rolling sequence of same-title rows is more likely a set of real
        # requisitions (Albertsons is a concrete example in the live snapshot).
        if len(posted_days) > 1 or any(not record.get("posted_at") for _, record in members):
            continue
        winner_key, winner = max(members, key=lambda pair: _winner_key(pair[1], pair[0]))
        merged = dict(winner)
        for key, record in members:
            if key == winner_key:
                continue
            reason = _posting_variant_reason(winner, record)
            if not reason:
                # Distinct locations are expected in this pass; the shared
                # board/title-family evidence is the location-variant proof.
                reason = "same aggregator board and marked title family; location variant"
            merged = _merge_record(merged, record, reason)
            aliases[key] = winner_key
            collapsed.pop(key, None)
            merged_count += 1
        merged["id"] = winner.get("id") or winner_key
        merged["posting_family_id"] = winner_key
        collapsed[winner_key] = merged
    return collapsed, aliases, merged_count


def resolve_alias(job_id: str, aliases: dict[str, str],
                  existing: dict | None = None) -> str:
    """Follow a persisted alias chain safely."""
    current = str(job_id)
    visited = set()
    while (current in aliases and current not in visited
           and current not in (existing or {})):
        visited.add(current)
        current = str(aliases[current])
    return current


def remap_entry_ids(entries: list[dict], aliases: dict[str, str],
                    jobs: dict | None = None) -> int:
    changed = 0
    for entry in entries:
        old = str(entry.get("id", ""))
        new = resolve_alias(old, aliases, existing=jobs)
        if old and new != old:
            entry["id"] = new
            entry["job_id_alias"] = old
            changed += 1
        record = (jobs or {}).get(new, {})
        official = record.get("url")
        known = [official, *(record.get("alternate_urls") or []),
                 (record.get("link_resolution") or {}).get("original_url")]
        entry_url = entry.get("url")
        entry_key = posting_key(entry_url)
        known_keys = {posting_key(url) for url in known if url}
        # An alias changes the durable ID, but it must not overwrite an owner
        # URL that does not belong to the surviving posting. Only promote to a
        # verified official URL when the old URL is empty or known provenance.
        # A remapped entry whose old URL is an explicitly supported
        # aggregator link is also safe to promote: the alias is precisely the
        # durable evidence that this discovery URL belonged to the posting.
        # Keep arbitrary owner-entered URLs untouched even when their ID was
        # later aliased.
        verified = (not entry_url or entry_key in known_keys or
                    (new != old and is_aggregator_url(entry_url)))
        if (verified and canonical_url(official)
                and not is_aggregator_url(official)
                and canonical_url(official) != canonical_url(entry_url)):
            if entry_url:
                links = list(entry.get("alternate_urls") or [])
                _append_unique(links, [entry_url])
                entry["alternate_urls"] = links
            entry["url"] = official
            changed += 1
    return changed


def remap_web_jobs(web: dict, aliases: dict[str, str],
                   existing: dict | None = None) -> int:
    """Move private workspace keys while preserving user notes and links."""
    jobs = web.get("jobs") or {}
    # The web map still contains alias keys until this function moves them;
    # only surviving keys should stop traversal. Callers with a durable jobs
    # map can pass it explicitly to apply the same rule across namespaces.
    stop_at = (existing if existing is not None else
               {key: value for key, value in jobs.items() if key not in aliases})
    remapped: dict[str, dict] = {}
    changed = 0
    for old, value in sorted(jobs.items(),
                             key=lambda pair: resolve_alias(
                                 str(pair[0]), aliases, existing=stop_at) != pair[0]):
        new = resolve_alias(str(old), aliases, existing=stop_at)
        if new != old:
            changed += 1
        current = remapped.setdefault(new, {})
        if isinstance(value, dict):
            for key, item in value.items():
                if key not in current or current[key] in (None, "", [], {}):
                    current[key] = item
                elif isinstance(item, list) and isinstance(current[key], list):
                    current[key] = list(current[key])
                    _append_unique(current[key], item)
                elif key == "notes" and item != current[key] and item:
                    current[key] = f"{current[key]}\n\n[Notes from merged job {old}]\n{item}"
                elif item != current[key]:
                    current.setdefault("merged_job_variants", []).append({
                        "id": old, "field": key, "value": item,
                    })
    if changed:
        web["jobs"] = remapped
    for field in ("maybe", "excluded"):
        original = web.get(field) or []
        remapped_ids = list(dict.fromkeys(
            resolve_alias(str(value), aliases, existing=stop_at)
            for value in original
        ))
        if original != remapped_ids:
            web[field] = remapped_ids
            changed += 1
    return changed
