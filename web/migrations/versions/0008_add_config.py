"""add config_versions and config_secrets, the config moves from config.toml into the app

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-23 20:01:41.853201
"""

import sqlalchemy as sa
from alembic import op

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('config_secrets',
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('value', sa.Text(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('updated_by', sa.String(length=320), nullable=True),
    sa.PrimaryKeyConstraint('name')
    )
    op.create_table('config_versions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('document', sa.JSON(), nullable=False),
    sa.Column('note', sa.String(length=500), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('created_by', sa.String(length=320), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    op.drop_table('config_versions')
    op.drop_table('config_secrets')
