"""add guide_snapshots, the TRaSH Guides' json at a commit

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-14 18:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = '0017'
down_revision = '0016'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('guide_snapshots',
    sa.Column('commit', sa.String(length=40), nullable=False),
    sa.Column('fetched_at', sa.DateTime(), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('commit')
    )
    with op.batch_alter_table('guide_snapshots') as batch_op:
        batch_op.create_index(batch_op.f('ix_guide_snapshots_fetched_at'), ['fetched_at'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('guide_snapshots') as batch_op:
        batch_op.drop_index(batch_op.f('ix_guide_snapshots_fetched_at'))
    op.drop_table('guide_snapshots')
