# alembic/versions/0003_late_ignored_index.py
"""late_ignored unique index

Revision ID: 0003
Revises: 0002
Create Date: 2026-01-01
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_ledger_late_ignored_per_task",
        "ledger",
        ["task_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'late_ignored'"),
    )


def downgrade() -> None:
    op.drop_index("uq_ledger_late_ignored_per_task", table_name="ledger")