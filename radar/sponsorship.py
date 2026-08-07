"""Official DOL LCA history for company-level sponsorship context.

This module intentionally does not make a job-specific sponsorship claim.  It
downloads the latest public OFLC LCA disclosure workbooks, aggregates certified
LCA history by normalized employer name, and stores a compact JSON index.  The
posting's own wording remains the authoritative per-role visa fact.

The raw DOL workbooks are large and are never committed.  Only the aggregate
index is written to ``state/sponsorship.json`` by the scheduled refresh job.
"""
from __future__ import annotations

import re
import tempfile
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin
import xml.etree.ElementTree as ET

import requests

from . import state
from .config import STATE_DIR, env
from .models import norm


DOL_PERFORMANCE_URL = "https://www.dol.gov/agencies/eta/foreign-labor/performance"
DOL_SOURCE_TITLE = "U.S. DOL OFLC LCA public disclosure data"
_MAIN_FILE_RE = re.compile(
    r"(?P<href>[^\"'<>]+LCA[^\"'<>]+FY(?P<fy>20\d{2})_Q(?P<q>[1-4])[^\"'<>]*\.xlsx)",
    re.I,
)
_XML_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_CELL_REF_RE = re.compile(r"([A-Z]+)\d+")
_CERTIFIED_RE = re.compile(r"^CERTIFIED(?:\s*-\s*WITHDRAWN)?$", re.I)
_CERTIFIED_WITHDRAWN_RE = re.compile(r"^CERTIFIED\s*-\s*WITHDRAWN$", re.I)

# Strip legal/entity noise before matching a public employer name to a radar
# company.  These are deliberately conservative; a fuzzy match could turn a
# historical filing for one employer into a sponsorship claim for another.
_ENTITY_NOISE = {
    "inc", "incorporated", "llc", "llp", "ltd", "limited", "corp", "corporation",
    "company", "co", "plc", "pc", "lp", "llc", "services", "service", "holdings",
    "holding", "group", "international", "global", "technologies", "technology",
    "systems", "solutions", "usa", "us", "america", "com",
}


def company_key(value: str) -> str:
    """Return a cautious legal-suffix-stripped employer key."""
    tokens = [token for token in norm(value).split() if token not in _ENTITY_NOISE]
    return " ".join(tokens)


def _prefixes(key: str) -> Iterable[str]:
    tokens = key.split()
    for length in range(2, len(tokens)):
        yield " ".join(tokens[:length])


def _cell_column(reference: str) -> str:
    match = _CELL_REF_RE.match(reference or "")
    return match.group(1) if match else ""


def _shared_strings(archive: zipfile.ZipFile) -> List[str]:
    values: List[str] = []
    with archive.open("xl/sharedStrings.xml") as stream:
        for _, element in ET.iterparse(stream, events=("end",)):
            if element.tag != _XML_NS + "si":
                continue
            values.append("".join(node.text or "" for node in element.iter(_XML_NS + "t")))
            element.clear()
    return values


def _cell_value(cell: ET.Element, shared: List[str]) -> str:
    value = cell.find(_XML_NS + "v")
    raw = value.text if value is not None else ""
    if cell.get("t") == "s" and raw:
        try:
            return shared[int(raw)]
        except (IndexError, ValueError):
            return ""
    if cell.get("t") == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(_XML_NS + "t"))
    return raw or ""


def _excel_date(value: str) -> str:
    try:
        date = datetime(1899, 12, 30, tzinfo=timezone.utc) + timedelta(days=float(value))
        return date.date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return ""


def _worker_count(value: str) -> int:
    try:
        return max(0, int(float(str(value).replace(",", "").strip())))
    except (TypeError, ValueError):
        return 0


def _read_workbook(path: Path, quarter: str, companies: Dict[str, dict]) -> int:
    """Aggregate one DOL workbook into ``companies``; return row count."""
    rows = 0
    with zipfile.ZipFile(path) as archive:
        shared = _shared_strings(archive)
        with archive.open("xl/worksheets/sheet1.xml") as stream:
            headers: Dict[str, str] = {}
            columns: Dict[str, str] = {}
            for _, row in ET.iterparse(stream, events=("end",)):
                if row.tag != _XML_NS + "row":
                    continue
                values = {
                    _cell_column(cell.get("r", "")): _cell_value(cell, shared)
                    for cell in row.findall(_XML_NS + "c")
                }
                row.clear()
                if not headers:
                    headers = values
                    columns = {
                        name: column for column, name in headers.items()
                        if name in {"VISA_CLASS", "EMPLOYER_NAME", "TRADE_NAME_DBA",
                                    "CASE_STATUS", "TOTAL_WORKER_POSITIONS", "DECISION_DATE"}
                    }
                    continue
                rows += 1
                visa = values.get(columns.get("VISA_CLASS", ""), "").strip().upper()
                if visa not in {"H-1B", "H-1B1", "E-3"}:
                    continue
                employer_column = columns.get("EMPLOYER_NAME", "")
                dba_column = columns.get("TRADE_NAME_DBA", "")
                status_column = columns.get("CASE_STATUS", "")
                workers_column = columns.get("TOTAL_WORKER_POSITIONS", "")
                decision_column = columns.get("DECISION_DATE", "")
                employer = values.get(employer_column, "").strip()
                dba = values.get(dba_column, "").strip()
                key = company_key(employer)
                if not key:
                    continue
                status = re.sub(r"\s+", " ", values.get(status_column, "").strip().upper())
                if not _CERTIFIED_RE.match(status):
                    continue
                record = companies.setdefault(key, {
                    "company_key": key,
                    "display_name": employer,
                    "aliases": [],
                    "filings": 0,
                    "certified_cases": 0,
                    "certified_withdrawn_cases": 0,
                    "certified_workers": 0,
                    "certified_withdrawn_workers": 0,
                    "quarters": [],
                    "latest_decision_date": "",
                })
                record["filings"] += 1
                worker_count = _worker_count(values.get(workers_column, ""))
                if _CERTIFIED_WITHDRAWN_RE.match(status):
                    record["certified_withdrawn_cases"] += 1
                    record["certified_withdrawn_workers"] += worker_count
                else:
                    record["certified_cases"] += 1
                    record["certified_workers"] += worker_count
                if quarter not in record["quarters"]:
                    record["quarters"].append(quarter)
                if employer and employer not in record["aliases"]:
                    record["aliases"].append(employer)
                if dba and dba not in record["aliases"]:
                    record["aliases"].append(dba)
                decision_date = _excel_date(values.get(decision_column, ""))
                if decision_date > str(record.get("latest_decision_date") or ""):
                    record["latest_decision_date"] = decision_date
    return rows


def discover_files(session=requests) -> List[dict]:
    """Find official quarterly LCA disclosure files from the DOL index."""
    response = session.get(DOL_PERFORMANCE_URL, timeout=30)
    response.raise_for_status()
    files: Dict[Tuple[int, int], dict] = {}
    for match in _MAIN_FILE_RE.finditer(response.text):
        href = match.group("href")
        basename = href.rsplit("/", 1)[-1].lower()
        if "appendix" in basename or "worksites" in basename:
            continue
        if "disclosure" not in basename and "dislclosure" not in basename:
            continue
        fy, quarter = int(match.group("fy")), int(match.group("q"))
        files[(fy, quarter)] = {
            "label": "FY%s Q%s" % (fy, quarter),
            "fiscal_year": fy,
            "quarter": quarter,
            "url": urljoin(DOL_PERFORMANCE_URL, href),
        }
    return [files[key] for key in sorted(files, reverse=True)]


def _history_status(record: Optional[dict]) -> str:
    if not record:
        return "no-history"
    if int(record.get("certified_cases", 0)) + int(record.get("certified_withdrawn_cases", 0)) > 0:
        return "likely"
    return "no-history"


def refresh(root: Optional[Path] = None, session=requests) -> dict:
    """Download and aggregate the newest DOL LCA quarters into state."""
    available = discover_files(session)
    limit = max(1, min(8, int(env("RADAR_SPONSORSHIP_QUARTERS", "4"))))
    selected = available[:limit]
    if not selected:
        raise RuntimeError("DOL performance page contained no LCA disclosure workbooks")
    companies: Dict[str, dict] = {}
    total_rows = 0
    with tempfile.TemporaryDirectory(prefix="radar-dol-lca-") as temp_dir:
        for item in selected:
            destination = Path(temp_dir) / (item["label"].replace(" ", "_") + ".xlsx")
            response = session.get(item["url"], stream=True, timeout=(30, 300))
            response.raise_for_status()
            with destination.open("wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        output.write(chunk)
            total_rows += _read_workbook(destination, item["label"], companies)
    for record in companies.values():
        record["status"] = _history_status(record)
        record["quarters"].sort(reverse=True)
        record["aliases"] = record["aliases"][:12]
    database = {
        "version": 1,
        "source": DOL_SOURCE_TITLE,
        "source_url": DOL_PERFORMANCE_URL,
        "retrieved_at": int(time.time()),
        "coverage_quarters": [item["label"] for item in selected],
        "files": selected,
        "companies": companies,
        "stats": {"rows_read": total_rows, "companies_with_certified_history": len(companies)},
    }
    return build_alias_index(database)


def load(root: Optional[Path] = None) -> dict:
    database = state.load("sponsorship.json", {})
    return build_alias_index(database) if database else {}


def _merge_records(records: List[dict]) -> dict:
    if not records:
        return {}
    result = {
        "status": "likely",
        "filings": sum(int(item.get("filings", 0)) for item in records),
        "certified_cases": sum(int(item.get("certified_cases", 0)) for item in records),
        "certified_withdrawn_cases": sum(int(item.get("certified_withdrawn_cases", 0)) for item in records),
        "certified_workers": sum(int(item.get("certified_workers", 0)) for item in records),
        "certified_withdrawn_workers": sum(int(item.get("certified_withdrawn_workers", 0)) for item in records),
        "aliases": sorted({alias for item in records for alias in item.get("aliases", [])})[:12],
        "quarters": sorted({quarter for item in records for quarter in item.get("quarters", [])}, reverse=True),
        "latest_decision_date": max(str(item.get("latest_decision_date") or "") for item in records),
    }
    return result


def history_for(company: str, database: Optional[dict] = None) -> dict:
    """Match one radar company to compact DOL history, conservatively."""
    database = database if database is not None else load()
    if not database or not database.get("companies"):
        return {"status": "unavailable"}
    key = company_key(company)
    companies = database.get("companies") or {}
    aliases: Dict[str, List[str]] = database.get("aliases") or {}
    direct = companies.get(key)
    if direct:
        return {**direct, "status": _history_status(direct)}
    matched = [companies[name] for name in aliases.get(key, []) if name in companies]
    if matched:
        return {**_merge_records(matched), "status": "likely"}
    # Prefix matching only handles a role board's location-qualified brand
    # (e.g. “Mayo Clinic Rochester” → “Mayo Clinic”). It requires two tokens.
    for prefix in reversed(list(_prefixes(key))):
        matched = [companies[name] for name in aliases.get(prefix, []) if name in companies]
        if matched:
            return {**_merge_records(matched), "status": "likely"}
    return {
        "status": "no-history",
        "quarters": list(database.get("coverage_quarters") or []),
    }


def annotate_record(record: dict, database: Optional[dict] = None) -> dict:
    """Attach history and one auditable context reason without changing score."""
    matched = history_for(str(record.get("company") or ""), database)
    coverage = list((database or {}).get("coverage_quarters") or matched.get("quarters") or [])
    # Jobs state is loaded by every platform visitor. Keep its per-job
    # annotation compact; the full employer aliases/filing record lives in the
    # separately refreshed sponsorship database.
    history = {"status": matched.get("status", "unavailable"),
               "coverage_quarters": coverage}
    for field in ("certified_cases", "certified_withdrawn_cases",
                  "certified_workers", "certified_withdrawn_workers",
                  "latest_decision_date"):
        if field in matched:
            history[field] = matched[field]
    record["sponsorship_history"] = history
    reasons = [reason for reason in record.get("score_reasons", [])
               if not str(reason).startswith("sponsor history:")]
    status = history.get("status")
    if status == "likely":
        cases = int(history.get("certified_cases", 0)) + int(history.get("certified_withdrawn_cases", 0))
        reasons.append(
            "sponsor history: likely — %s certified LCA case(s); company context only, "
            "not a promise for this posting" % cases)
    elif status == "no-history":
        reasons.append(
            "sponsor history: no-history in the covered DOL quarters; this is not a "
            "finding that the company will not sponsor")
    else:
        reasons.append("sponsor history: unavailable — DOL refresh has not completed")
    record["score_reasons"] = reasons
    return record


def build_alias_index(database: dict) -> dict:
    """Build compact runtime aliases after loading/generated state."""
    aliases: Dict[str, List[str]] = {}
    for key, record in (database.get("companies") or {}).items():
        candidates = [key] + [company_key(alias) for alias in record.get("aliases", [])]
        for candidate in candidates:
            if not candidate:
                continue
            for alias in [candidate, *_prefixes(candidate)]:
                if key not in aliases.setdefault(alias, []):
                    aliases[alias].append(key)
    database["aliases"] = aliases
    return database


def refresh_command(root: Optional[Path] = None) -> int:
    database = refresh(root)
    state.save("sponsorship.json", database)
    print("sponsorship: %s companies with certified DOL history across %s; %s rows read"
          % (database["stats"]["companies_with_certified_history"],
             ", ".join(database["coverage_quarters"]), database["stats"]["rows_read"]))
    return 0
