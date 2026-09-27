"""allow_unencrypted_backup

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-28 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('backup_settings', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'allow_unencrypted_backup',
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table('backup_settings', schema=None) as batch_op:
        batch_op.drop_column('allow_unencrypted_backup')
