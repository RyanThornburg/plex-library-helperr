"""alembic env: runs against the connection web.db.migrate passes in"""

from alembic import context

from web.db import Base

connection = context.config.attributes["connection"]
context.configure(
    connection=connection,
    target_metadata=Base.metadata,
    # sqlite can't alter most columns in place, batch mode recreates the table
    render_as_batch=True,
)
with context.begin_transaction():
    context.run_migrations()
