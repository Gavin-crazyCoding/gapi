"""add_daily_login_and_fingerprint

Revision ID: 5cd96097227c
Revises: 6335443d7199
Create Date: 2026-09-12 14:27:53.334080
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = '5cd96097227c'
down_revision: str | None = '6335443d7199'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add missing columns to users
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('concurrent_active', sa.Integer(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('last_login_date', sa.DateTime(timezone=True), nullable=True))

    # Create email_verifications table
    op.create_table('email_verifications',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('token', sa.String(length=64), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token')
    )
    with op.batch_alter_table('email_verifications', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_email_verifications_user_id'), ['user_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('email_verifications', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_email_verifications_user_id'))

    op.drop_table('email_verifications')
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('last_login_date')
        batch_op.drop_column('concurrent_active')
