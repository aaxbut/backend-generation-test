"""init schema

Revision ID: 0001
Revises:
Create Date: 2026-01-01
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("balance", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "models",
        sa.Column("slug", sa.String(64), primary_key=True),
        sa.Column("price", sa.Integer, nullable=False),
    )

    op.create_table(
        "legs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "model_slug",
            sa.String(64),
            sa.ForeignKey("models.slug", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("leg_index", sa.Integer, nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("max_refs", sa.Integer, nullable=False),
        sa.Column("skip_on_moderation", sa.Boolean, nullable=False, server_default="false"),
        sa.UniqueConstraint("model_slug", "leg_index", name="uq_leg_model_index"),
    )

    task_status = sa.Enum("running", "completed", "failed", name="task_status")
    attempt_status = sa.Enum(
        "pending", "completed", "failed", "timed_out", name="attempt_status"
    )
    error_type = sa.Enum("technical", "moderation", "payload", name="error_type")
    ledger_kind = sa.Enum(
        "charge", "refund", "topup", "late_ignored", name="ledger_kind"
    )

    op.create_table(
        "tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "model_slug",
            sa.String(64),
            sa.ForeignKey("models.slug", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("prompt", sa.Text, nullable=False),
        sa.Column("ref_images", sa.Integer, nullable=False),
        sa.Column("price", sa.Integer, nullable=False),
        sa.Column("status", task_status, nullable=False, server_default="running"),
        sa.Column("current_leg_index", sa.Integer, nullable=True),
        sa.Column("result_url", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_tasks_user_active",
        "tasks",
        ["user_id"],
        postgresql_where=sa.text("status = 'running'"),
    )

    op.create_table(
        "attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("leg_index", sa.Integer, nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("status", attempt_status, nullable=False, server_default="pending"),
        sa.Column("external_id", sa.String(128), nullable=True),
        sa.Column("error_type", error_type, nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("result_url", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("task_id", "leg_index", name="uq_attempt_task_leg"),
    )
    op.create_index("ix_attempt_external", "attempts", ["provider", "external_id"])
    op.create_index(
        "ix_attempt_pending_deadline",
        "attempts",
        ["deadline_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        "payments",
        sa.Column("payment_id", sa.String(128), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("tokens", sa.BigInteger, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "ledger",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("kind", ledger_kind, nullable=False),
        sa.Column("delta", sa.BigInteger, nullable=False),
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("payment_id", sa.String(128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_ledger_user_created", "ledger", ["user_id", "created_at"])
    # Частичные уникальные индексы — страховка «ровно один раз».
    op.create_index(
        "uq_ledger_charge_per_task",
        "ledger",
        ["task_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'charge'"),
    )
    op.create_index(
        "uq_ledger_refund_per_task",
        "ledger",
        ["task_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'refund'"),
    )
    op.create_index(
        "uq_ledger_topup_per_payment",
        "ledger",
        ["payment_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'topup'"),
    )


def downgrade() -> None:
    op.drop_table("ledger")
    op.drop_table("payments")
    op.drop_table("attempts")
    op.drop_table("tasks")
    op.drop_table("legs")
    op.drop_table("models")
    op.drop_table("users")
    # Enum-типы в PG отдельные, дропаем явно.
    for name in ("ledger_kind", "error_type", "attempt_status", "task_status"):
        op.execute(f"DROP TYPE IF EXISTS {name}")