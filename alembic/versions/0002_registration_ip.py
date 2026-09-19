"""add users.registration_ip

Revision ID: 0002_registration_ip
Revises: 0001
"""

from alembic import op
import sqlalchemy as sa

revision = "0002_registration_ip"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable: accounts created before this column existed have no recorded IP,
    # and the one-per-IP check treats NULL as "unknown", never as a match.
    op.add_column(
        "users",
        sa.Column("registration_ip", sa.String(length=45), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "registration_ip")
