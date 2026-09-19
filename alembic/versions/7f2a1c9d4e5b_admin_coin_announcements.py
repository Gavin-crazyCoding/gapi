"""admin: system_settings, announcements, api_key quota, last_announcement_at

Revision ID: 7f2a1c9d4e5b
Revises: 5cd96097227c
Create Date: 2026-09-13 12:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = '7f2a1c9d4e5b'
down_revision: str | None = '5cd96097227c'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # users.last_announcement_at — marks the newest announcement a user has seen
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('last_announcement_at', sa.DateTime(timezone=True), nullable=True))

    # api_keys.quota + expires_at
    with op.batch_alter_table('api_keys', schema=None) as batch_op:
        batch_op.add_column(sa.Column('quota', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True))

    # system_settings — editable coin/exchange config
    op.create_table(
        'system_settings',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('key', sa.String(length=64), nullable=False),
        sa.Column('value', sa.Text(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('key'),
    )

    # announcements — site-wide banners
    op.create_table(
        'announcements',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('as_html', sa.Boolean(), server_default='0', nullable=False),
        sa.Column('start_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('end_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('priority', sa.Integer(), server_default='0', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )

    # Seed defaults into system_settings so admin edits have a stable base.
    op.bulk_insert(
        sa.table(
            'system_settings',
            sa.column('key', sa.String),
            sa.column('value', sa.Text),
            sa.column('updated_at', sa.DateTime),
        ),
        [
            {"key": "coin_rate", "value": "100", "updated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc)},
            {"key": "daily_bonus", "value": "1", "updated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc)},
            {"key": "max_coin_per_user", "value": "1000000", "updated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc)},
            {"key": "registration_bonus", "value": "20", "updated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc)},
        ],
    )


def downgrade() -> None:
    op.drop_table('announcements')
    op.drop_table('system_settings')
    with op.batch_alter_table('api_keys', schema=None) as batch_op:
        batch_op.drop_column('expires_at')
        batch_op.drop_column('quota')
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('last_announcement_at')