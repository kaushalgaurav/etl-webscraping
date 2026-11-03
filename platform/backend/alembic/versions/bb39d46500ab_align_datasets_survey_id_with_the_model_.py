"""align datasets.survey_id with the model (BIGINT -> INTEGER)

Revision ID: bb39d46500ab
Revises: e32fa9bdbdbf
Create Date: 2026-10-05 22:39:43.567279

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'bb39d46500ab'
down_revision: Union[str, Sequence[str], None] = 'e32fa9bdbdbf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Narrow datasets.survey_id from BIGINT to INTEGER.

    Corrects a column added by hand on the deployed PostgreSQL database before
    migrations existed. The baseline already creates it as INTEGER, so on a new
    database there is nothing to do — and SQLite has no ALTER COLUMN TYPE at
    all, which would otherwise break the chain on the SQLite fallback.
    """
    if op.get_bind().dialect.name != "postgresql":
        return
    op.alter_column('datasets', 'survey_id',
               existing_type=sa.BIGINT(),
               type_=sa.Integer(),
               existing_nullable=True)


def downgrade() -> None:
    """Widen it back. PostgreSQL only, for the same reason as upgrade()."""
    if op.get_bind().dialect.name != "postgresql":
        return
    op.alter_column('datasets', 'survey_id',
               existing_type=sa.Integer(),
               type_=sa.BIGINT(),
               existing_nullable=True)
