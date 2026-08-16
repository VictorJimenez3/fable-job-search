"""Typed operator CLI. Legacy single-word commands remain in radar.main."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path

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
