"""seed catalog: models and legs

Revision ID: 0002
Revises: 0001
Create Date: 2026-01-01
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


MODELS = [
    # slug, price (токенов)
    ("image-fast", 2),
    ("video-long", 25),
]

LEGS = [
    # model_slug, leg_index, provider, max_refs, skip_on_moderation
    ("image-fast", 0, "alpha", 4, False),
    ("image-fast", 1, "beta", 10, False),
    ("video-long", 0, "beta", 2, False),
    ("video-long", 1, "alpha", 1, True),
]


def upgrade() -> None:
    models_t = sa.table(
        "models",
        sa.column("slug", sa.String),
        sa.column("price", sa.Integer),
    )
    legs_t = sa.table(
        "legs",
        sa.column("model_slug", sa.String),
        sa.column("leg_index", sa.Integer),
        sa.column("provider", sa.String),
        sa.column("max_refs", sa.Integer),
        sa.column("skip_on_moderation", sa.Boolean),
    )
    op.bulk_insert(models_t, [{"slug": s, "price": p} for s, p in MODELS])
    op.bulk_insert(
        legs_t,
        [
            {
                "model_slug": m,
                "leg_index": i,
                "provider": p,
                "max_refs": r,
                "skip_on_moderation": k,
            }
            for m, i, p, r, k in LEGS
        ],
    )


def downgrade() -> None:
    op.execute("DELETE FROM legs")
    op.execute("DELETE FROM models")