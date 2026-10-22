"""add schedules and job_runs.schedule_id

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-22 12:58:08.207710
"""

import sqlalchemy as sa
from alembic import op

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('schedules',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('job', sa.String(length=32), nullable=False),
    sa.Column('cron', sa.String(length=100), nullable=False),
    sa.Column('dry_run', sa.Boolean(), server_default=sa.text('1'), nullable=False),
    sa.Column('enabled', sa.Boolean(), server_default=sa.text('1'), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('last_fired_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('job_runs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('schedule_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_job_runs_schedule_id'), ['schedule_id'], unique=False)
        batch_op.create_foreign_key('fk_job_runs_schedule_id', 'schedules', ['schedule_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    with op.batch_alter_table('job_runs', schema=None) as batch_op:
        batch_op.drop_constraint('fk_job_runs_schedule_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_job_runs_schedule_id'))
        batch_op.drop_column('schedule_id')

    op.drop_table('schedules')
