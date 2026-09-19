"""add_email_verified_concurrent_fingerprint

Revision ID: 6335443d7199
Revises: 0005_package_tokens
Create Date: 2026-09-12 13:27:22.319007
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = '6335443d7199'
down_revision: str | None = '0005_package_tokens'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('email_verified', sa.Boolean(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('concurrent_limit', sa.Integer(), server_default='5', nullable=False))
        batch_op.add_column(sa.Column('fingerprint_hash', sa.String(length=64), nullable=True))

def downgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('fingerprint_hash')
        batch_op.drop_column('concurrent_limit')
        batch_op.drop_column('email_verified')