"""plaintext_config: drop backup ciphertext columns, secrets move to config/secrets.json

Also drops the unused agent_tokens table (agent auth no longer reads it).
Drops email_accounts/notification_settings ciphertext columns and disables
both so credentials must be re-entered through the plaintext vault.
Financial tables (statements/transactions/repayments) are untouched.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-25
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Credentials no longer exist after this migration; force scheduled
    # backups off until the user re-enters them via the settings page.
    op.execute(
        "UPDATE backup_settings SET schedule_type = 'off', webdav_verified_at = NULL"
    )
    with op.batch_alter_table("backup_settings") as batch_op:
        batch_op.drop_column("backup_password_ciphertext")
        batch_op.drop_column("webdav_password_ciphertext")

    op.execute("UPDATE email_accounts SET enabled = 0")
    with op.batch_alter_table("email_accounts") as batch_op:
        batch_op.drop_column("secret_ciphertext")

    op.execute("UPDATE notification_settings SET enabled = 0")
    with op.batch_alter_table("notification_settings") as batch_op:
        batch_op.drop_column("secret_ciphertext")

    op.drop_index("ix_agent_tokens_token_hash", table_name="agent_tokens")
    op.drop_table("agent_tokens")


def downgrade() -> None:
    with op.batch_alter_table("backup_settings") as batch_op:
        batch_op.add_column(
            sa.Column("backup_password_ciphertext", sa.Text(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("webdav_password_ciphertext", sa.Text(), nullable=True)
        )
    with op.batch_alter_table("notification_settings") as batch_op:
        batch_op.add_column(
            sa.Column("secret_ciphertext", sa.Text(), nullable=True)
        )
    with op.batch_alter_table("email_accounts") as batch_op:
        batch_op.add_column(
            sa.Column(
                "secret_ciphertext", sa.Text(), nullable=False, server_default=""
            )
        )
    op.create_table(
        "agent_tokens",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("secret_ciphertext", sa.Text(), nullable=True),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_tokens_token_hash", "agent_tokens", ["token_hash"], unique=True
    )
