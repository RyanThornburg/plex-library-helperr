"""add playbacks, the last few weeks of plays with how plex streamed them

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-26 21:18:20.264163
"""

import sqlalchemy as sa
from alembic import op

revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('playbacks',
    sa.Column('id', sa.Integer(), autoincrement=False, nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('user', sa.String(length=255), nullable=False),
    sa.Column('media_type', sa.String(length=16), nullable=False),
    sa.Column('rating_key', sa.Integer(), nullable=True),
    sa.Column('title', sa.String(length=500), nullable=False),
    sa.Column('platform', sa.String(length=64), nullable=True),
    sa.Column('product', sa.String(length=64), nullable=True),
    sa.Column('player', sa.String(length=255), nullable=True),
    sa.Column('location', sa.String(length=16), nullable=True),
    sa.Column('transcode_decision', sa.String(length=32), nullable=True),
    sa.Column('details', sa.Boolean(), nullable=False),
    sa.Column('quality_profile', sa.String(length=64), nullable=True),
    sa.Column('container', sa.String(length=32), nullable=True),
    sa.Column('stream_container', sa.String(length=32), nullable=True),
    sa.Column('container_decision', sa.String(length=32), nullable=True),
    sa.Column('video_decision', sa.String(length=32), nullable=True),
    sa.Column('video_codec', sa.String(length=32), nullable=True),
    sa.Column('stream_video_codec', sa.String(length=32), nullable=True),
    sa.Column('video_resolution', sa.String(length=16), nullable=True),
    sa.Column('stream_video_resolution', sa.String(length=16), nullable=True),
    sa.Column('video_dynamic_range', sa.String(length=32), nullable=True),
    sa.Column('stream_video_dynamic_range', sa.String(length=32), nullable=True),
    sa.Column('audio_decision', sa.String(length=32), nullable=True),
    sa.Column('audio_codec', sa.String(length=32), nullable=True),
    sa.Column('stream_audio_codec', sa.String(length=32), nullable=True),
    sa.Column('audio_channels', sa.Integer(), nullable=True),
    sa.Column('stream_audio_channels', sa.Integer(), nullable=True),
    sa.Column('subtitle_decision', sa.String(length=32), nullable=True),
    sa.Column('subtitle_codec', sa.String(length=32), nullable=True),
    sa.Column('bitrate', sa.Integer(), nullable=True),
    sa.Column('stream_bitrate', sa.Integer(), nullable=True),
    sa.Column('hw_decoding', sa.Boolean(), nullable=True),
    sa.Column('hw_encoding', sa.Boolean(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('playbacks', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_playbacks_started_at'), ['started_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_playbacks_user_id'), ['user_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('playbacks', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_playbacks_user_id'))
        batch_op.drop_index(batch_op.f('ix_playbacks_started_at'))

    op.drop_table('playbacks')
