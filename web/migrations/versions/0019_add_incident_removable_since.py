"""add health_incidents.removable_since, when a stuck download became one queue cleanup
removes

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-26 12:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = '0019'
down_revision = '0018'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('health_incidents') as batch_op:
        batch_op.add_column(sa.Column('removable_since', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('health_incidents') as batch_op:
        batch_op.drop_column('removable_since')
