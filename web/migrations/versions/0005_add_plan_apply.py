"""add job_runs.plan_run_id and approved, for applying a reviewed removers plan

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-22 17:45:26.944502
"""

import sqlalchemy as sa
from alembic import op

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('job_runs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('plan_run_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('approved', sa.JSON(), nullable=True))
        batch_op.create_index(batch_op.f('ix_job_runs_plan_run_id'), ['plan_run_id'], unique=False)
        batch_op.create_foreign_key('fk_job_runs_plan_run_id', 'job_runs', ['plan_run_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    with op.batch_alter_table('job_runs', schema=None) as batch_op:
        batch_op.drop_constraint('fk_job_runs_plan_run_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_job_runs_plan_run_id'))
        batch_op.drop_column('approved')
        batch_op.drop_column('plan_run_id')
