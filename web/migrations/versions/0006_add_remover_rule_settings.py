"""add remover_rule_settings, for auto-approving removers

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-22 21:40:59.407517
"""

import sqlalchemy as sa
from alembic import op

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('remover_rule_settings',
    sa.Column('rule_key', sa.String(length=1000), nullable=False),
    sa.Column('auto_approve', sa.Boolean(), server_default=sa.text('0'), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('updated_by', sa.String(length=320), nullable=True),
    sa.PrimaryKeyConstraint('rule_key')
    )


def downgrade() -> None:
    op.drop_table('remover_rule_settings')
