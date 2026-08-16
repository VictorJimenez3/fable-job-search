"""Create the vNext normalized datastore.

Revision ID: 0001_vnext
Revises:
Create Date: 2026-08-16
"""
from __future__ import annotations

from alembic import op

from radar.db.schema import metadata

revision = "0001_vnext"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    metadata.drop_all(bind=op.get_bind(), checkfirst=True)
