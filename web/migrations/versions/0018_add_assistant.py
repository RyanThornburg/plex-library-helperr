"""add assistant_chats, assistant_messages and assistant_actions, the Assistant page's chats

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-19 12:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = '0018'
down_revision = '0017'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('assistant_chats',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('created_by', sa.String(length=320), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('input_tokens', sa.Integer(), server_default='0', nullable=False),
    sa.Column('output_tokens', sa.Integer(), server_default='0', nullable=False),
    sa.Column('cache_read_tokens', sa.Integer(), server_default='0', nullable=False),
    sa.Column('cache_write_tokens', sa.Integer(), server_default='0', nullable=False),
    sa.Column('tool_calls', sa.Integer(), server_default='0', nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('assistant_chats') as batch_op:
        batch_op.create_index(batch_op.f('ix_assistant_chats_updated_at'), ['updated_at'], unique=False)

    op.create_table('assistant_messages',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('chat_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('content', sa.JSON(), nullable=False),
    sa.Column('text', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['chat_id'], ['assistant_chats.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('assistant_messages') as batch_op:
        batch_op.create_index(batch_op.f('ix_assistant_messages_chat_id'), ['chat_id'], unique=False)

    op.create_table('assistant_actions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('chat_id', sa.Integer(), nullable=False),
    sa.Column('tool_use_id', sa.String(length=64), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('params', sa.JSON(), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('result', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('acted_at', sa.DateTime(), nullable=True),
    sa.Column('acted_by', sa.String(length=320), nullable=True),
    sa.Column('reported', sa.Boolean(), server_default=sa.text('0'), nullable=False),
    sa.ForeignKeyConstraint(['chat_id'], ['assistant_chats.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('assistant_actions') as batch_op:
        batch_op.create_index(batch_op.f('ix_assistant_actions_chat_id'), ['chat_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('assistant_actions') as batch_op:
        batch_op.drop_index(batch_op.f('ix_assistant_actions_chat_id'))
    op.drop_table('assistant_actions')
    with op.batch_alter_table('assistant_messages') as batch_op:
        batch_op.drop_index(batch_op.f('ix_assistant_messages_chat_id'))
    op.drop_table('assistant_messages')
    with op.batch_alter_table('assistant_chats') as batch_op:
        batch_op.drop_index(batch_op.f('ix_assistant_chats_updated_at'))
    op.drop_table('assistant_chats')
