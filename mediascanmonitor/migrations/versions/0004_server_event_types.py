"""server.event_types

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-15 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("server", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "event_types",
                sqlmodel.sql.sqltypes.AutoString(),
                nullable=False,
                server_default="created,moved_to,deleted,moved_from",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("server", schema=None) as batch_op:
        batch_op.drop_column("event_types")
