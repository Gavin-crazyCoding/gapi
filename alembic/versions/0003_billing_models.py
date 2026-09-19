"""billing: token_packages, usage_records, credit_transactions, pricing

Revision ID: 0003_billing
Revises: 0002_registration_ip
"""

from alembic import op
import sqlalchemy as sa

revision = "0003_billing"
down_revision = "0002_registration_ip"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "token_packages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("total_tokens", sa.Integer(), nullable=False),
        sa.Column("remaining_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_gavincoin", sa.Numeric(18, 6), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_token_packages_user_id", "token_packages", ["user_id"])

    op.create_table(
        "usage_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("api_key_id", sa.Integer(), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("endpoint", sa.String(length=128), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("gavincoin_cost", sa.Numeric(18, 6), nullable=False, server_default="0"),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="ok"),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["api_key_id"], ["api_keys.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_usage_records_user_id", "usage_records", ["user_id"])
    op.create_index("ix_usage_records_model", "usage_records", ["model"])
    op.create_index("ix_usage_records_request_id", "usage_records", ["request_id"])

    op.create_table(
        "credit_transactions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(18, 6), nullable=False),
        sa.Column("balance_after", sa.Numeric(18, 6), nullable=False),
        sa.Column("tx_type", sa.String(length=32), nullable=False),
        sa.Column("reference_id", sa.String(length=64), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_credit_transactions_user_id", "credit_transactions", ["user_id"])
    op.create_index("ix_credit_transactions_tx_type", "credit_transactions", ["tx_type"])

    op.create_table(
        "pricing",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("model", sa.String(length=128), unique=True, nullable=False),
        sa.Column("input_per_1k", sa.Numeric(18, 6), nullable=False),
        sa.Column("output_per_1k", sa.Numeric(18, 6), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_pricing_model", "pricing", ["model"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_pricing_model", table_name="pricing")
    op.drop_table("pricing")
    op.drop_index("ix_credit_transactions_tx_type", table_name="credit_transactions")
    op.drop_index("ix_credit_transactions_user_id", table_name="credit_transactions")
    op.drop_table("credit_transactions")
    op.drop_index("ix_usage_records_request_id", table_name="usage_records")
    op.drop_index("ix_usage_records_model", table_name="usage_records")
    op.drop_index("ix_usage_records_user_id", table_name="usage_records")
    op.drop_table("usage_records")
    op.drop_index("ix_token_packages_user_id", table_name="token_packages")
    op.drop_table("token_packages")