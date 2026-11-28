"""add play_sessions, Plex sessions from Tautulli's playback webhooks

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-28 12:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = '0020'
down_revision = '0019'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('play_sessions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('session_key', sa.String(length=64), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=False),
    sa.Column('ended_at', sa.DateTime(), nullable=True),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('user', sa.String(length=255), nullable=False),
    sa.Column('rating_key', sa.Integer(), nullable=True),
    sa.Column('title', sa.String(length=500), nullable=False),
    sa.Column('media_type', sa.String(length=16), nullable=True),
    sa.Column('player', sa.String(length=255), nullable=True),
    sa.Column('product', sa.String(length=64), nullable=True),
    sa.Column('platform', sa.String(length=64), nullable=True),
    sa.Column('location', sa.String(length=16), nullable=True),
    sa.Column('quality_profile', sa.String(length=64), nullable=True),
    sa.Column('transcode_decision', sa.String(length=32), nullable=True),
    sa.Column('video_decision', sa.String(length=32), nullable=True),
    sa.Column('audio_decision', sa.String(length=32), nullable=True),
    sa.Column('subtitle_decision', sa.String(length=32), nullable=True),
    sa.Column('container', sa.String(length=32), nullable=True),
    sa.Column('video_codec', sa.String(length=32), nullable=True),
    sa.Column('video_resolution', sa.String(length=16), nullable=True),
    sa.Column('audio_codec', sa.String(length=32), nullable=True),
    sa.Column('audio_channels', sa.Integer(), nullable=True),
    sa.Column('audio_profile', sa.String(length=64), nullable=True),
    sa.Column('start_offset', sa.Integer(), nullable=True),
    sa.Column('end_offset', sa.Integer(), nullable=True),
    sa.Column('stream_seconds', sa.Integer(), nullable=True),
    sa.Column('error', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('play_sessions') as batch_op:
        batch_op.create_index(batch_op.f('ix_play_sessions_session_key'), ['session_key'], unique=False)
        batch_op.create_index(batch_op.f('ix_play_sessions_started_at'), ['started_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_play_sessions_user_id'), ['user_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('play_sessions') as batch_op:
        batch_op.drop_index(batch_op.f('ix_play_sessions_user_id'))
        batch_op.drop_index(batch_op.f('ix_play_sessions_started_at'))
        batch_op.drop_index(batch_op.f('ix_play_sessions_session_key'))
    op.drop_table('play_sessions')
