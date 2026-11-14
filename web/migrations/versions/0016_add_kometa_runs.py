"""add kometa_runs, Kometa's runs from its webhooks

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-14 12:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = '0016'
down_revision = '0015'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('kometa_runs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=False),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.Column('run_seconds', sa.Integer(), nullable=True),
    sa.Column('counts', sa.JSON(), nullable=True),
    sa.Column('errors', sa.JSON(), nullable=False),
    sa.Column('version', sa.String(length=64), nullable=True),
    sa.Column('latest_version', sa.String(length=64), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('kometa_runs') as batch_op:
        batch_op.create_index(batch_op.f('ix_kometa_runs_started_at'), ['started_at'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('kometa_runs') as batch_op:
        batch_op.drop_index(batch_op.f('ix_kometa_runs_started_at'))
    op.drop_table('kometa_runs')
