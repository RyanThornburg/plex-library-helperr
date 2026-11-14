"""add library_items, everyone's plays of each movie, show and season in plex

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-13 12:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = '0015'
down_revision = '0014'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('library_items',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=8), nullable=False),
    sa.Column('rating_key', sa.Integer(), nullable=False),
    sa.Column('season', sa.Integer(), nullable=True),
    sa.Column('library', sa.String(length=255), nullable=False),
    sa.Column('title', sa.String(length=500), nullable=False),
    sa.Column('year', sa.Integer(), nullable=True),
    sa.Column('tmdb_id', sa.Integer(), nullable=True),
    sa.Column('tvdb_id', sa.Integer(), nullable=True),
    sa.Column('added_at', sa.DateTime(), nullable=True),
    sa.Column('episodes', sa.Integer(), nullable=True),
    sa.Column('plays', sa.Integer(), nullable=False),
    sa.Column('viewers', sa.Integer(), nullable=False),
    sa.Column('last_played_at', sa.DateTime(), nullable=True),
    sa.Column('last_viewer', sa.String(length=255), nullable=True),
    sa.Column('synced_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('library_items') as batch_op:
        batch_op.create_index(batch_op.f('ix_library_items_kind'), ['kind'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('library_items') as batch_op:
        batch_op.drop_index(batch_op.f('ix_library_items_kind'))
    op.drop_table('library_items')
