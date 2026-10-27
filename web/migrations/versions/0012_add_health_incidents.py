"""add health_incidents, the problems health checks saw and when they cleared

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-26 23:03:10.593264
"""

import sqlalchemy as sa
from alembic import op

revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('health_incidents',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('service', sa.String(length=16), nullable=False),
    sa.Column('key', sa.String(length=500), nullable=False),
    sa.Column('level', sa.String(length=8), nullable=False),
    sa.Column('text', sa.String(length=1000), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(), nullable=False),
    sa.Column('ended_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('health_incidents', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_health_incidents_ended_at'), ['ended_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_health_incidents_service'), ['service'], unique=False)
        batch_op.create_index(batch_op.f('ix_health_incidents_started_at'), ['started_at'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('health_incidents', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_health_incidents_started_at'))
        batch_op.drop_index(batch_op.f('ix_health_incidents_service'))
        batch_op.drop_index(batch_op.f('ix_health_incidents_ended_at'))

    op.drop_table('health_incidents')
