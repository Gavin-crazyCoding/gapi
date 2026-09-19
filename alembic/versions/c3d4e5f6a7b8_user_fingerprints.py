"""user_fingerprints table (multi-device binding), drop users.fingerprint_hash

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-09-18 12:10:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = 'c3d4e5f6a7b8'
down_revision: str | None = 'b2c3d4e5f6a7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'user_fingerprints',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('fp_hash', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.UniqueConstraint('user_id', 'fp_hash', name='uq_user_fingerprint'),
    )
    op.create_index('ix_user_fingerprints_user_id', 'user_fingerprints', ['user_id'])
    op.create_index('ix_user_fingerprints_fp_hash', 'user_fingerprints', ['fp_hash'])

    # Carry the single stored hash over as the first binding row.
    op.execute(
        """
        INSERT INTO user_fingerprints (user_id, fp_hash, created_at, last_seen_at)
        SELECT id, fingerprint_hash, created_at, created_at FROM users
        WHERE fingerprint_hash IS NOT NULL
        """
    )

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('fingerprint_hash')


def downgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('fingerprint_hash', sa.String(length=64), nullable=True))
    op.execute(
        """
        UPDATE users SET fingerprint_hash = (
            SELECT fp_hash FROM user_fingerprints
            WHERE user_fingerprints.user_id = users.id
            ORDER BY last_seen_at DESC LIMIT 1
        )
        """
    )
    op.drop_index('ix_user_fingerprints_fp_hash', table_name='user_fingerprints')
    op.drop_index('ix_user_fingerprints_user_id', table_name='user_fingerprints')
    op.drop_table('user_fingerprints')
