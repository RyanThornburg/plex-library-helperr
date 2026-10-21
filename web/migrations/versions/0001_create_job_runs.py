"""create job_runs

Revision ID: 0001
Revises:
Create Date: 2026-10-21 16:53:19.291466
"""

import sqlalchemy as sa
from alembic import op

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('job_runs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('job', sa.String(length=32), nullable=False),
    sa.Column('trigger', sa.String(length=16), nullable=False),
    sa.Column('requested_by', sa.String(length=320), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=True),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.Column('result', sa.JSON(), nullable=True),
    sa.Column('log', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('job_runs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_job_runs_status'), ['status'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('job_runs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_job_runs_status'))

    op.drop_table('job_runs')
