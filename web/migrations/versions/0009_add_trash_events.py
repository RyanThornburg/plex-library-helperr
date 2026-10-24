"""add trash_events, upgrades from radarr/sonarr webhooks

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-24 15:12:35.419932
"""

import sqlalchemy as sa
from alembic import op

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('trash_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('received_at', sa.DateTime(), nullable=False),
    sa.Column('event', sa.String(length=32), nullable=False),
    sa.Column('source', sa.String(length=320), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('trash_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_trash_events_received_at'), ['received_at'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('trash_events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_trash_events_received_at'))

    op.drop_table('trash_events')
