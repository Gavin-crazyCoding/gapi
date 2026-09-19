"""usage_records.package_tokens: tokens covered by prepaid packages

Revision ID: 0005_package_tokens
Revises: 42efb92571ef
"""

from alembic import op
import sqlalchemy as sa

revision = "0005_package_tokens"
down_revision = "42efb92571ef"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "usage_records",
        sa.Column(
            "package_tokens",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("usage_records", "package_tokens")
