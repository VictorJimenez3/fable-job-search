"""Small transactional repository used by workers, APIs, and migration tools."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, create_engine, func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from .schema import (
    application_events,
    applications,
    companies,
    posting_aliases,
    posting_sightings,
    postings,
    profiles,
    score_snapshots,
    source_boards,
    work_items,
)


def database_url() -> str:
    return os.environ.get("DATABASE_URL", "").strip()


class RadarRepository:
    def __init__(self, engine: Engine):
        self.engine = engine

    @contextmanager
    def transaction(self) -> Iterator:
        with self.engine.begin() as connection:
            yield connection

    def ensure_profile(self, profile_id: str, display_name: str, config: dict) -> None:
        statement = insert(profiles).values(id=profile_id, display_name=display_name, config=config)
        statement = statement.on_conflict_do_update(
            index_elements=[profiles.c.id],
            set_={"display_name": statement.excluded.display_name, "config": statement.excluded.config},
        )
        with self.transaction() as connection:
            connection.execute(statement)

    def ensure_company(self, display_name: str, metadata: dict | None = None) -> uuid.UUID:
        normalized = " ".join(display_name.casefold().split())[:240] or "unknown"
        company_id = uuid.uuid5(uuid.NAMESPACE_URL, f"job-radar-company:{normalized}")
        base_statement = insert(companies).values(
            id=company_id,
            normalized_name=normalized,
            display_name=display_name[:240] or "Unknown",
            metadata=metadata or {},
        )
        statement = base_statement.on_conflict_do_update(
            index_elements=[companies.c.normalized_name],
            set_={
                "display_name": base_statement.excluded.display_name,
                "metadata": base_statement.excluded.metadata,
                "updated_at": func.now(),
            },
        ).returning(companies.c.id)
        with self.transaction() as connection:
            return connection.execute(statement).scalar_one()

    def upsert_posting(self, record: dict) -> uuid.UUID:
        posting_id = record.get("id") or uuid.uuid4()
        values = {**record, "id": posting_id}
        base_statement = insert(postings).values(**values)
        mutable = {
            key: getattr(base_statement.excluded, key)
            for key in values
            if key not in {"id", "public_id", "first_seen_at"}
        }
        statement = base_statement.on_conflict_do_update(
            index_elements=[postings.c.public_id], set_=mutable
        ).returning(postings.c.id)
        with self.transaction() as connection:
            return connection.execute(statement).scalar_one()

    def add_alias(self, alias: str, posting_id: uuid.UUID, kind: str) -> None:
        statement = insert(posting_aliases).values(alias=alias, posting_id=posting_id, kind=kind)
        statement = statement.on_conflict_do_update(
            index_elements=[posting_aliases.c.alias],
            set_={"posting_id": posting_id, "kind": kind},
        )
        with self.transaction() as connection:
            connection.execute(statement)

    def ensure_source_board(self, board: dict) -> uuid.UUID:
        board_id = board.get("id") or uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"job-radar-source:{board['profile_id']}:{board['namespace']}:{board.get('tenant', '')}",
        )
        base_statement = insert(source_boards).values(**{**board, "id": board_id})
        statement = base_statement.on_conflict_do_update(
            constraint="uq_source_board_identity",
            set_={
                "board_url": base_statement.excluded.board_url,
                "enabled": base_statement.excluded.enabled,
            },
        ).returning(source_boards.c.id)
        with self.transaction() as connection:
            return connection.execute(statement).scalar_one()

    def upsert_sighting(self, sighting: dict) -> None:
        statement = insert(posting_sightings).values(**{**sighting, "id": sighting.get("id") or uuid.uuid4()})
        statement = statement.on_conflict_do_update(
            constraint="uq_posting_sighting",
            set_={
                "posting_id": statement.excluded.posting_id,
                "source_url": statement.excluded.source_url,
                "payload_hash": statement.excluded.payload_hash,
                "last_seen_at": statement.excluded.last_seen_at,
            },
        )
        with self.transaction() as connection:
            connection.execute(statement)

    def upsert_score(self, snapshot: dict) -> None:
        statement = (
            insert(score_snapshots)
            .values(**{**snapshot, "id": snapshot.get("id") or uuid.uuid4()})
            .on_conflict_do_nothing(constraint="uq_score_snapshot")
        )
        with self.transaction() as connection:
            connection.execute(statement)

    def resolve_posting(self, identifier: str) -> dict | None:
        query = select(postings).where(postings.c.public_id == identifier)
        with self.engine.connect() as connection:
            row = connection.execute(query).mappings().first()
            if row:
                return dict(row)
            alias = connection.execute(
                select(posting_aliases.c.posting_id).where(posting_aliases.c.alias == identifier)
            ).scalar_one_or_none()
            if alias is None:
                return None
            found = connection.execute(select(postings).where(postings.c.id == alias)).mappings().first()
            return dict(found) if found else None

    def record_application_event(self, application: dict, event: dict) -> uuid.UUID:
        application_id = application.get("id") or uuid.uuid4()
        with self.transaction() as connection:
            app_statement = insert(applications).values(**{**application, "id": application_id})
            conflict_target = (
                {"constraint": "uq_application_posting_profile"}
                if application.get("posting_id") is not None
                else {"index_elements": [applications.c.id]}
            )
            upsert_statement = app_statement.on_conflict_do_update(
                **conflict_target,
                set_={
                    "current_stage": app_statement.excluded.current_stage,
                    "updated_at": app_statement.excluded.updated_at,
                    "external_links": app_statement.excluded.external_links,
                },
            ).returning(applications.c.id)
            application_id = connection.execute(upsert_statement).scalar_one()
            event_statement = (
                insert(application_events)
                .values(**{**event, "id": event.get("id") or uuid.uuid4(), "application_id": application_id})
                .on_conflict_do_nothing(index_elements=[application_events.c.idempotency_key])
            )
            connection.execute(event_statement)
        return application_id

    def enqueue_work(self, kind: str, payload: dict, idempotency_key: str, priority: int = 0) -> uuid.UUID:
        item_id = uuid.uuid4()
        statement = (
            insert(work_items)
            .values(
                id=item_id,
                kind=kind[:80],
                payload=payload,
                priority=priority,
                idempotency_key=idempotency_key[:200],
            )
            .on_conflict_do_update(
                index_elements=[work_items.c.idempotency_key],
                set_={
                    "payload": payload,
                    "priority": priority,
                    "available_at": func.least(work_items.c.available_at, func.now()),
                },
            )
            .returning(work_items.c.id)
        )
        with self.transaction() as connection:
            return connection.execute(statement).scalar_one()

    def lease_work(self, owner: str, lease_seconds: int = 900) -> dict | None:
        lease_seconds = max(30, min(3600, int(lease_seconds)))
        statement = text("""
            with candidate as (
              select id from work_items
              where status = 'queued' and available_at <= now()
                and (lease_expires_at is null or lease_expires_at < now())
              order by priority desc, created_at
              for update skip locked limit 1
            )
            update work_items w set status = 'leased', lease_owner = :owner,
              lease_expires_at = now() + make_interval(secs => :lease_seconds),
              attempts = attempts + 1
            from candidate where w.id = candidate.id
            returning w.*
        """)
        with self.transaction() as connection:
            row = connection.execute(
                statement, {"owner": owner[:120], "lease_seconds": lease_seconds}
            ).mappings().first()
            return dict(row) if row else None

    def finish_work(self, item_id: uuid.UUID, owner: str, *, error: str = "", max_attempts: int = 5) -> None:
        now = datetime.now(UTC)
        with self.transaction() as connection:
            current = connection.execute(
                select(work_items).where(
                    work_items.c.id == item_id,
                    work_items.c.status == "leased",
                    work_items.c.lease_owner == owner[:120],
                )
            ).mappings().first()
            if not current:
                raise RuntimeError("work item lease is no longer owned by this worker")
            if not error:
                values: dict[str, object] = {
                    "status": "complete",
                    "lease_owner": None,
                    "lease_expires_at": None,
                }
            elif int(current["attempts"]) >= max(1, max_attempts):
                values = {
                    "status": "failed",
                    "payload": {**dict(current["payload"] or {}), "last_error": error[:500]},
                    "lease_owner": None,
                    "lease_expires_at": None,
                }
            else:
                delay = min(3600, 30 * (2 ** max(0, int(current["attempts"]) - 1)))
                values = {
                    "status": "queued",
                    "payload": {**dict(current["payload"] or {}), "last_error": error[:500]},
                    "available_at": now + timedelta(seconds=delay),
                    "lease_owner": None,
                    "lease_expires_at": None,
                }
            connection.execute(update(work_items).where(work_items.c.id == item_id).values(**values))


def repository(url: str = "") -> RadarRepository:
    target = url or database_url()
    if not target:
        raise RuntimeError("DATABASE_URL is required for Postgres operations")
    return RadarRepository(create_engine(target, pool_pre_ping=True))
