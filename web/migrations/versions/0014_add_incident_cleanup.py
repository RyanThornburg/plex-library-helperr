"""add health_incidents.cleanup, what queue cleanup did with a stuck download

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-11 12:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('health_incidents') as batch_op:
        batch_op.add_column(sa.Column('cleanup', sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column('cleanup_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('health_incidents') as batch_op:
        batch_op.drop_column('cleanup_at')
        batch_op.drop_column('cleanup')
