"""announcements.is_active, pricing.flat_fee_coin, ledger reference_id index

Revision ID: a1b2c3d4e5f6
Revises: 7f2a1c9d4e5b
Create Date: 2026-09-17 23:30:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = 'a1b2c3d4e5f6'
down_revision: str | None = '7f2a1c9d4e5b'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('announcements', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('is_active', sa.Boolean(), server_default='1', nullable=False)
        )
    with op.batch_alter_table('pricing', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('flat_fee_coin', sa.Numeric(18, 6), nullable=True)
        )
    # The ORM has had index=True on credit_transactions.reference_id since the
    # billing migration, but the index was never actually created.
    op.create_index(
        'ix_credit_transactions_reference_id',
        'credit_transactions',
        ['reference_id'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_credit_transactions_reference_id', table_name='credit_transactions')
    with op.batch_alter_table('pricing', schema=None) as batch_op:
        batch_op.drop_column('flat_fee_coin')
    with op.batch_alter_table('announcements', schema=None) as batch_op:
        batch_op.drop_column('is_active')
