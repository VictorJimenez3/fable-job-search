"""Canonical SQLAlchemy Core schema shared by workers, migrations, and importers."""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

metadata = MetaData()

AUTH_UUID_DEFAULT = text("gen_random_uuid()")

# Better Auth core schema. Field/table names are explicitly mapped in
# ``webapp/auth.config.mjs`` so migrations stay owned by Alembic rather than a
# second schema tool. OAuth tokens are encrypted by Better Auth before storage.
auth_users = Table(
    "auth_users",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=AUTH_UUID_DEFAULT),
    Column("name", String(255), nullable=False),
    Column("email", String(320), nullable=False, unique=True),
    Column("email_verified", Boolean, nullable=False, server_default="false"),
    Column("image", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

auth_sessions = Table(
    "auth_sessions",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=AUTH_UUID_DEFAULT),
    Column("user_id", ForeignKey("auth_users.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("token", Text, nullable=False, unique=True),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("ip_address", Text),
    Column("user_agent", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

auth_accounts = Table(
    "auth_accounts",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=AUTH_UUID_DEFAULT),
    Column("user_id", ForeignKey("auth_users.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("account_id", Text, nullable=False),
    Column("provider_id", String(80), nullable=False),
    Column("access_token", Text),
    Column("refresh_token", Text),
    Column("id_token", Text),
    Column("access_token_expires_at", DateTime(timezone=True)),
    Column("refresh_token_expires_at", DateTime(timezone=True)),
    Column("scope", Text),
    Column("password", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("provider_id", "account_id", name="uq_auth_provider_account"),
)

auth_verifications = Table(
    "auth_verifications",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=AUTH_UUID_DEFAULT),
    Column("identifier", Text, nullable=False, index=True),
    Column("value", Text, nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

auth_rate_limits = Table(
    "auth_rate_limits",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=AUTH_UUID_DEFAULT),
    Column("key", Text, nullable=False, unique=True),
    Column("count", Integer, nullable=False),
    Column("last_request", BigInteger, nullable=False),
)

profiles = Table(
    "profiles",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("display_name", String(120), nullable=False),
    Column("config", JSON, nullable=False, server_default="{}"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

companies = Table(
    "companies",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("normalized_name", String(240), nullable=False, unique=True),
    Column("display_name", String(240), nullable=False),
    Column("website", Text),
    Column("metadata", JSON, nullable=False, server_default="{}"),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

source_boards = Table(
    "source_boards",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("namespace", String(80), nullable=False),
    Column("tenant", String(240), nullable=False, server_default=""),
    Column("board_url", Text),
    Column("profile_id", ForeignKey("profiles.id"), nullable=False),
    Column("enabled", Boolean, nullable=False, server_default="true"),
    Column("cursor", JSON, nullable=False, server_default="{}"),
    Column("health", JSON, nullable=False, server_default="{}"),
    UniqueConstraint("namespace", "tenant", "profile_id", name="uq_source_board_identity"),
)

source_runs = Table(
    "source_runs",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("board_id", ForeignKey("source_boards.id"), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True)),
    Column("status", String(24), nullable=False),
    Column("counts", JSON, nullable=False, server_default="{}"),
    Column("errors", JSON, nullable=False, server_default="[]"),
    Column("cursor", JSON, nullable=False, server_default="{}"),
    CheckConstraint("status in ('running','succeeded','partial','failed')", name="ck_source_run_status"),
)

postings = Table(
    "postings",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("public_id", String(40), nullable=False, unique=True),
    Column("profile_id", ForeignKey("profiles.id"), nullable=False),
    Column("company_id", ForeignKey("companies.id")),
    Column("company", String(240), nullable=False),
    Column("title", String(500), nullable=False),
    Column("canonical_url", Text, nullable=False),
    Column("locations", JSON, nullable=False, server_default="[]"),
    Column("remote", Boolean, nullable=False, server_default="false"),
    Column("salary", Text, nullable=False, server_default=""),
    Column("sector", String(80), nullable=False, server_default=""),
    Column("posted_at", DateTime(timezone=True)),
    Column("first_seen_at", DateTime(timezone=True), nullable=False),
    Column("last_seen_at", DateTime(timezone=True), nullable=False),
    Column("status", String(24), nullable=False, server_default="open"),
    Column("status_reason", Text, nullable=False, server_default=""),
    Column("posting_facts", JSON, nullable=False, server_default="{}"),
    Column("metadata", JSON, nullable=False, server_default="{}"),
    CheckConstraint("status in ('open','expired','filled','archived')", name="ck_posting_status"),
)
Index("ix_postings_action_queue", postings.c.profile_id, postings.c.status, postings.c.last_seen_at)
Index("ix_postings_company", postings.c.company)

posting_aliases = Table(
    "posting_aliases",
    metadata,
    Column("alias", String(200), primary_key=True),
    Column("posting_id", ForeignKey("postings.id", ondelete="CASCADE"), nullable=False),
    Column("kind", String(40), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

posting_sightings = Table(
    "posting_sightings",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("posting_id", ForeignKey("postings.id", ondelete="CASCADE"), nullable=False),
    Column("board_id", ForeignKey("source_boards.id"), nullable=False),
    Column("external_id", String(300), nullable=False),
    Column("source_url", Text),
    Column("payload_hash", String(64), nullable=False),
    Column("first_seen_at", DateTime(timezone=True), nullable=False),
    Column("last_seen_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("board_id", "external_id", name="uq_posting_sighting"),
)

posting_status_events = Table(
    "posting_status_events",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("posting_id", ForeignKey("postings.id", ondelete="CASCADE"), nullable=False),
    Column("status", String(24), nullable=False),
    Column("reason", Text, nullable=False, server_default=""),
    Column("source_run_id", ForeignKey("source_runs.id")),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
)

score_snapshots = Table(
    "score_snapshots",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("posting_id", ForeignKey("postings.id", ondelete="CASCADE"), nullable=False),
    Column("profile_id", ForeignKey("profiles.id"), nullable=False),
    Column("version", String(40), nullable=False),
    Column("input_hash", String(64), nullable=False),
    Column("evidence_score", Integer, nullable=False),
    Column("raw_score", Float, nullable=False),
    Column("eligibility", String(20), nullable=False),
    Column("priority_tier", String(20), nullable=False),
    Column("dimensions", JSON, nullable=False, server_default="{}"),
    Column("reasons", JSON, nullable=False, server_default="[]"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("posting_id", "profile_id", "version", "input_hash", name="uq_score_snapshot"),
    CheckConstraint("evidence_score between 0 and 100", name="ck_evidence_score"),
)

applications = Table(
    "applications",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("posting_id", ForeignKey("postings.id")),
    Column("profile_id", ForeignKey("profiles.id"), nullable=False),
    Column("current_stage", String(32), nullable=False),
    Column("company", String(240), nullable=False),
    Column("title", String(500), nullable=False),
    Column("url", Text),
    Column("external_links", JSON, nullable=False, server_default="{}"),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("profile_id", "posting_id", name="uq_application_posting_profile"),
)

application_events = Table(
    "application_events",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("application_id", ForeignKey("applications.id", ondelete="CASCADE"), nullable=False),
    Column("stage", String(32), nullable=False),
    Column("origin", String(80), nullable=False),
    Column("idempotency_key", String(200), nullable=False, unique=True),
    Column("external_revision", String(200)),
    Column("metadata", JSON, nullable=False, server_default="{}"),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
)


def _event_table(name: str) -> Table:
    return Table(
        name,
        metadata,
        Column("id", UUID(as_uuid=True), primary_key=True),
        Column("profile_id", ForeignKey("profiles.id")),
        Column("kind", String(80), nullable=False),
        Column("payload", JSON, nullable=False, server_default="{}"),
        Column("idempotency_key", String(200), unique=True),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    )


preferences = _event_table("preferences")
feedback_events = _event_table("feedback_events")
company_research_versions = _event_table("company_research_versions")
notification_outbox = _event_table("notification_outbox")
automation_runs = _event_table("automation_runs")
prompt_versions = _event_table("prompt_versions")
llm_runs = _event_table("llm_runs")

work_items = Table(
    "work_items",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("kind", String(80), nullable=False),
    Column("payload", JSON, nullable=False, server_default="{}"),
    Column("status", String(24), nullable=False, server_default="queued"),
    Column("priority", Integer, nullable=False, server_default="0"),
    Column("available_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("lease_owner", String(120)),
    Column("lease_expires_at", DateTime(timezone=True)),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("idempotency_key", String(200), nullable=False, unique=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("status in ('queued','leased','complete','failed')", name="ck_work_item_status"),
)
