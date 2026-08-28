"""Typed operator CLI. Legacy single-word commands remain in radar.main."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Optional

import typer

from . import main as legacy
from .config import profile_id

app = typer.Typer(no_args_is_help=True, help="Job Radar operator CLI")
crawl_app = typer.Typer(help="Run discovery and scoring")
source_app = typer.Typer(help="Manage source registries and health")
score_app = typer.Typer(help="Recompute and explain deterministic scores")
lifecycle_app = typer.Typer(help="Reconcile posting lifecycle")
enrich_app = typer.Typer(help="Enrich postings and companies")
tracker_app = typer.Typer(help="Synchronize application trackers")
notify_app = typer.Typer(help="Dispatch alerts and digests")
db_app = typer.Typer(help="Migrate and verify Postgres state")
studio_app = typer.Typer(help="Run the private Resume Studio")
worker_app = typer.Typer(help="Run durable Postgres work items")

for name, group in (
    ("crawl", crawl_app),
    ("source", source_app),
    ("score", score_app),
    ("lifecycle", lifecycle_app),
    ("enrich", enrich_app),
    ("tracker", tracker_app),
    ("notify", notify_app),
    ("db", db_app),
    ("resume-studio", studio_app),
    ("worker", worker_app),
):
    app.add_typer(group, name=name)


def _run(label: str, fn: Callable[[], object], dry_run: bool, json_output: bool) -> None:
    if dry_run:
        value = {"ok": True, "dry_run": True, "operation": label, "profile": profile_id()}
    else:
        result = fn()
        value = {"ok": result in (None, 0, True), "operation": label, "result": result}
    if json_output:
        typer.echo(json.dumps(value, default=str, sort_keys=True))
    elif dry_run:
        typer.echo(f"dry-run: {label} for {value['profile']}")
    elif not value["ok"]:
        result = value.get("result")
        code = result if isinstance(result, int) and not isinstance(result, bool) else 1
        raise typer.Exit(code=code)


@crawl_app.command("run")
def crawl_run(
    profile: str = typer.Option("", help="Profile override"),
    dry_run: bool = typer.Option(False),
    json_output: bool = typer.Option(False, "--json"),
    limit: int = typer.Option(0, min=0),
) -> None:
    if profile:
        os.environ["RADAR_PROFILE"] = profile
    if limit:
        os.environ["RADAR_SOURCE_LIMIT"] = str(limit)
    _run("crawl.run", legacy.crawl, dry_run, json_output)


@source_app.command("seed")
def source_seed(dry_run: bool = False, json_output: bool = typer.Option(False, "--json")) -> None:
    _run("source.seed", legacy.seed_cmd, dry_run, json_output)


@source_app.command("probe")
def source_probe(dry_run: bool = False, json_output: bool = typer.Option(False, "--json")) -> None:
    _run("source.probe", legacy.resolve_links_cmd, dry_run, json_output)


@source_app.command("health")
def source_health(json_output: bool = typer.Option(False, "--json")) -> None:
    from . import state

    runs = state.load("runs.json", [])
    value = {"profile": profile_id(), "latest": runs[-1] if runs else None, "run_count": len(runs)}
    typer.echo(
        json.dumps(value, default=str, sort_keys=True) if json_output else json.dumps(value, default=str, indent=2)
    )


@score_app.command("recompute")
def score_recompute(dry_run: bool = False, json_output: bool = typer.Option(False, "--json")) -> None:
    _run("score.recompute", legacy.rescore_cmd, dry_run, json_output)


@score_app.command("health")
def score_health(json_output: bool = typer.Option(False, "--json")) -> None:
    _run("score.health", legacy.score_health_cmd, False, json_output)


@score_app.command("explain")
def score_explain(job_id: str, json_output: bool = typer.Option(False, "--json")) -> None:
    from . import state

    record = state.jobs().get(job_id)
    if not record:
        raise typer.BadParameter("unknown job id")
    value = {
        key: record.get(key)
        for key in (
            "id", "company", "title", "score", "evidence_score", "eligibility",
            "priority_tier", "score_raw", "score_dimensions", "score_reasons", "alert_ok",
        )
    }
    typer.echo(json.dumps(value, indent=None if json_output else 2, sort_keys=True))


@lifecycle_app.command("reconcile")
def lifecycle_reconcile(dry_run: bool = False, json_output: bool = typer.Option(False, "--json")) -> None:
    _run("lifecycle.reconcile", legacy.lifecycle_cmd, dry_run, json_output)


@enrich_app.command("jobs")
def enrich_jobs(dry_run: bool = False, json_output: bool = typer.Option(False, "--json")) -> None:
    _run("enrich.jobs", legacy.enrich, dry_run, json_output)


@enrich_app.command("companies")
def enrich_companies(dry_run: bool = False, json_output: bool = typer.Option(False, "--json")) -> None:
    _run("enrich.companies", legacy.enrich, dry_run, json_output)


@tracker_app.command("sync")
def tracker_sync(dry_run: bool = False, json_output: bool = typer.Option(False, "--json")) -> None:
    _run("tracker.sync", legacy.tracker_sync, dry_run, json_output)


@tracker_app.command("import")
def tracker_import(dry_run: bool = False, json_output: bool = typer.Option(False, "--json")) -> None:
    _run("tracker.import", legacy.notion_backfill, dry_run, json_output)


@tracker_app.command("export")
def tracker_export(json_output: bool = typer.Option(False, "--json")) -> None:
    from . import state

    value = state.applied()
    typer.echo(json.dumps(value, indent=None if json_output else 2, sort_keys=True))


@notify_app.command("dispatch")
def notify_dispatch(dry_run: bool = False, json_output: bool = typer.Option(False, "--json")) -> None:
    _run("notify.dispatch", legacy.deliver_alerts, dry_run, json_output)


@notify_app.command("daily")
def notify_daily(dry_run: bool = False, json_output: bool = typer.Option(False, "--json")) -> None:
    from .board import post_daily_best
    from .state import jobs

    _run("notify.daily", lambda: post_daily_best(jobs()), dry_run, json_output)


@db_app.command("migrate")
def db_migrate(sql: bool = typer.Option(False, help="Print SQL without applying")) -> None:
    from alembic import command
    from alembic.config import Config

    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    if sql:
        command.upgrade(config, "head", sql=True)
    else:
        command.upgrade(config, "head")


@db_app.command("import-legacy")
def db_import_legacy(json_output: bool = typer.Option(False, "--json")) -> None:
    from .db.import_legacy import import_legacy

    value = import_legacy()
    typer.echo(json.dumps(value, indent=None if json_output else 2, default=str, sort_keys=True))


@db_app.command("parity")
def db_parity(json_output: bool = typer.Option(False, "--json")) -> None:
    from .db.import_legacy import parity

    value = parity()
    typer.echo(json.dumps(value, indent=None if json_output else 2, default=str, sort_keys=True))
    if not all(value["matches"].values()):
        raise typer.Exit(2)


@db_app.command("export-snapshot")
def db_export_snapshot() -> None:
    typer.echo("snapshot export is produced by the maintenance workflow after Postgres cutover")


@worker_app.command("run")
def worker_run(
    once: bool = typer.Option(False, help="Process at most one available item"),
    limit: int = typer.Option(25, min=1, max=1000),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    from .db.repository import repository
    from .worker import run_one, worker_name

    repo = repository()
    owner = worker_name()
    processed = 0
    for _ in range(1 if once else limit):
        if not run_one(repo, owner=owner):
            break
        processed += 1
    value = {"ok": True, "processed": processed, "worker": owner}
    typer.echo(json.dumps(value, sort_keys=True) if json_output else f"worker: processed {processed} item(s)")


@studio_app.command("serve")
def studio_serve(host: str = "127.0.0.1", port: int = 4317) -> None:
    from scripts.resume_studio import main as studio_main

    raise typer.Exit(studio_main(["--host", host, "--port", str(port)]))


@studio_app.command("export")
def studio_export(
    since_days: int = typer.Option(
        14, "--since-days", min=0, max=3650,
        help="Keep the newest PDF per company from this many recent days.",
    ),
    all_history: bool = typer.Option(
        False, "--all-history",
        help="Include the newest usable run for every company in the private history.",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    from scripts.resume_studio import export_local_tailored_resumes

    value = export_local_tailored_resumes(
        since_days=None if all_history else since_days,
    )
    if json_output:
        typer.echo(json.dumps(value, default=str, sort_keys=True))
    else:
        typer.echo(f"Exported {value['count']} resume(s) to {value['folder']}")


@studio_app.command("bank")
def studio_bank(
    query: str = typer.Option("", "--query", "-q", help="Filter by company, title, or sector."),
    approved_only: bool = typer.Option(
        False, "--approved-only", help="Show only explicitly approved tailored winners.",
    ),
    limit: int = typer.Option(25, min=1, max=500),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List unique reusable PDFs in the local Resume Bank."""
    from scripts.resume_studio import resume_bank

    value = resume_bank(query=query, approved_only=approved_only, limit=limit)
    if json_output:
        typer.echo(json.dumps(value, default=str, sort_keys=True))
        return
    typer.echo(
        "Resume Bank: %d unique PDF(s), %d approved, %d need review"
        % (
            value["unique_resumes"], value["approved_resumes"],
            value["review_required_resumes"],
        )
    )
    for entry in value["entries"]:
        state = "APPROVED" if entry["safe_for_application"] else "REVIEW"
        typer.echo(
            "%s  %s  %s — %s\n  %s"
            % (
                state, entry["run_id"], entry["company"] or "Unknown company",
                entry["title"] or "Unknown role", entry["pdf_filename"],
            )
        )


@studio_app.command("offline-tailor")
def studio_offline_tailor(
    job_id: str = typer.Option("", "--job-id", help="Use a role from local state/jobs.json."),
    company: str = typer.Option("", "--company", help="Target company for an ad-hoc role."),
    title: str = typer.Option("", "--title", help="Target job title for an ad-hoc role."),
    description_file: Optional[Path] = typer.Option(
        None, "--description-file", exists=True, dir_okay=False, readable=True,
        help="Optional plain-text job description used for better matching.",
    ),
    include_review: bool = typer.Option(
        False, "--include-review",
        help="Allow review-pending/legacy PDFs; inspect before applying.",
    ),
    copy_pdf: bool = typer.Option(
        True, "--copy/--no-copy", help="Copy the winner into CV/tailored/offline/.",
    ),
    top: int = typer.Option(5, "--top", min=1, max=20),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Select the best existing resume for a role without calling Codex."""
    from scripts.resume_studio import load_jobs, offline_tailor_resume

    job = {}
    if job_id:
        job = dict(load_jobs().get(job_id) or {})
        if not job:
            raise typer.BadParameter("unknown job id", param_hint="--job-id")
    if company:
        job["company"] = company
    if title:
        job["title"] = title
    if description_file is not None:
        job["description"] = description_file.read_text(errors="replace")
    if not str(job.get("company") or "").strip() or not str(job.get("title") or "").strip():
        raise typer.BadParameter(
            "provide --job-id, or provide both --company and --title",
        )
    value = offline_tailor_resume(
        job, approved_only=not include_review, limit=top, copy_pdf=copy_pdf,
    )
    if json_output:
        typer.echo(json.dumps(value, default=str, sort_keys=True))
    elif value.get("selected"):
        selected = value["selected"]
        typer.echo(
            "Offline match: %.2f  %s — %s\nSource run: %s (%s)\nOutput: %s\nCodex calls: 0"
            % (
                float(selected.get("score") or 0), selected.get("company") or "Unknown company",
                selected.get("title") or "Unknown role", selected.get("run_id") or "",
                selected.get("pdf_filename") or "resume PDF",
                value.get("output_path") or "not copied (--no-copy)",
            )
        )
        for reason in selected.get("reasons") or []:
            typer.echo("  - %s" % reason)
        if selected.get("needs_owner_review"):
            typer.echo("WARNING: this source is not approved; review it before applying.", err=True)
    else:
        typer.echo(
            "No approved reusable resume is available. Run `resume-studio bank`, approve a ready run, "
            "or repeat with --include-review for an inspection-only match.",
            err=True,
        )
        raise typer.Exit(2)


@studio_app.command("approve")
def studio_approve(
    run_id: str = typer.Argument(..., help="12-character Resume Studio run ID."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Approve a ready PDF after personally reviewing its gate report."""
    from scripts.resume_studio import approve_run

    try:
        value = approve_run(None, run_id)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="run_id") from exc
    if json_output:
        typer.echo(json.dumps(value, default=str, sort_keys=True))
    else:
        typer.echo("Approved %s: %s" % (run_id, value.get("pdf_filename") or "resume PDF"))


@studio_app.command("usage")
def studio_usage(json_output: bool = typer.Option(False, "--json")) -> None:
    """Show observed local provider usage (offline commands always use zero)."""
    from scripts.resume_studio import studio_usage as usage_summary

    value = usage_summary()
    if json_output:
        typer.echo(json.dumps(value, default=str, sort_keys=True))
    else:
        typer.echo(
            "Since %s: %d Codex call(s), %d observed token(s).\n%s\n"
            "Bank, export, approve, and offline-tailor do not call Codex."
            % (
                value["week_start"], value["codex_calls"], value["codex_tokens"],
                value["note"],
            )
        )


@app.command("doctor")
def doctor(json_output: bool = typer.Option(False, "--json")) -> None:
    from .db.repository import database_url

    checks = {
        "python": sys.version.split()[0],
        "profile": profile_id(),
        "database_configured": bool(database_url()),
        "state_directory": str(Path(__file__).resolve().parents[1] / "state"),
        "ai_optional": True,
    }
    typer.echo(json.dumps(checks, indent=None if json_output else 2, sort_keys=True))


if __name__ == "__main__":
    app()
