"""add plays and media_requests, for request stats

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-24 16:48:26.431989
"""

import sqlalchemy as sa
from alembic import op

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('media_requests',
    sa.Column('id', sa.Integer(), autoincrement=False, nullable=False),
    sa.Column('media_type', sa.String(length=8), nullable=False),
    sa.Column('tmdb_id', sa.Integer(), nullable=True),
    sa.Column('tvdb_id', sa.Integer(), nullable=True),
    sa.Column('title', sa.String(length=500), nullable=False),
    sa.Column('year', sa.Integer(), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('media_status', sa.String(length=32), nullable=False),
    sa.Column('is_4k', sa.Boolean(), nullable=False),
    sa.Column('seasons', sa.JSON(), nullable=False),
    sa.Column('requested_at', sa.DateTime(), nullable=False),
    sa.Column('available_at', sa.DateTime(), nullable=True),
    sa.Column('requester_id', sa.Integer(), nullable=False),
    sa.Column('requester', sa.String(length=255), nullable=False),
    sa.Column('requester_user_id', sa.Integer(), nullable=True),
    sa.Column('items', sa.Integer(), nullable=False),
    sa.Column('watched', sa.Integer(), nullable=False),
    sa.Column('started', sa.Integer(), nullable=False),
    sa.Column('requester_plays', sa.Integer(), nullable=False),
    sa.Column('last_played_at', sa.DateTime(), nullable=True),
    sa.Column('other_viewers', sa.Integer(), nullable=False),
    sa.Column('synced_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('media_requests', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_media_requests_requested_at'), ['requested_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_media_requests_requester_id'), ['requester_id'], unique=False)

    op.create_table('plays',
    sa.Column('id', sa.Integer(), autoincrement=False, nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=False),
    sa.Column('stopped_at', sa.DateTime(), nullable=True),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('username', sa.String(length=255), nullable=False),
    sa.Column('media_type', sa.String(length=16), nullable=False),
    sa.Column('rating_key', sa.Integer(), nullable=True),
    sa.Column('grandparent_rating_key', sa.Integer(), nullable=True),
    sa.Column('season', sa.Integer(), nullable=True),
    sa.Column('episode', sa.Integer(), nullable=True),
    sa.Column('guid', sa.String(length=255), nullable=False),
    sa.Column('title', sa.String(length=500), nullable=False),
    sa.Column('percent_complete', sa.Integer(), nullable=False),
    sa.Column('watched', sa.Boolean(), nullable=False),
    sa.Column('play_duration', sa.Integer(), nullable=True),
    sa.Column('transcode_decision', sa.String(length=32), nullable=True),
    sa.Column('platform', sa.String(length=64), nullable=True),
    sa.Column('product', sa.String(length=64), nullable=True),
    sa.Column('player', sa.String(length=255), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('plays', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_plays_grandparent_rating_key'), ['grandparent_rating_key'], unique=False)
        batch_op.create_index(batch_op.f('ix_plays_guid'), ['guid'], unique=False)
        batch_op.create_index(batch_op.f('ix_plays_started_at'), ['started_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_plays_user_id'), ['user_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('plays', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_plays_user_id'))
        batch_op.drop_index(batch_op.f('ix_plays_started_at'))
        batch_op.drop_index(batch_op.f('ix_plays_guid'))
        batch_op.drop_index(batch_op.f('ix_plays_grandparent_rating_key'))

    op.drop_table('plays')
    with op.batch_alter_table('media_requests', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_media_requests_requester_id'))
        batch_op.drop_index(batch_op.f('ix_media_requests_requested_at'))

    op.drop_table('media_requests')
