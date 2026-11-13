"""website warehouse and scrape base

Creates web_tables / web_raw / web_clean / web_master, and moves scrape_scans
and scrape_findings onto the Website-table base (BIGINT id, UUID external_id,
created_at, updated_at), keeping saved Searches.

Hand-written parts (autogenerate can't convert ids that rows point at):
  1. unsaved Searches are deleted: their staging folders are named by the old
     ids, so they could never be opened again (the app's startup sweep then
     removes the folders);
  2. both tables get a new BIGSERIAL id and a generated external_id; findings
     are re-pointed to the new ids through the old ones, then the old ids go.
The web tables are created last: web_tables.finding_id points at the new id.
One-way: downgrade drops the web tables and then refuses.

Revision ID: 05f280b1545e
Revises: b32eb5b4e8f3
Create Date: 2026-11-05 15:17:28.680105

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '05f280b1545e'
down_revision: Union[str, Sequence[str], None] = 'b32eb5b4e8f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 1. Unsaved Searches can't survive the id change.
    op.execute("DELETE FROM scrape_findings WHERE scan_id IN "
               "(SELECT id FROM scrape_scans WHERE status <> 'imported')")
    op.execute("DELETE FROM scrape_scans WHERE status <> 'imported'")

    # 2. Set the old ids aside.
    op.drop_constraint('scrape_findings_scan_id_fkey', 'scrape_findings', type_='foreignkey')
    op.drop_constraint('scrape_findings_pkey', 'scrape_findings', type_='primary')
    op.drop_index('ix_scrape_findings_scan_id', table_name='scrape_findings')
    op.alter_column('scrape_findings', 'id', new_column_name='old_id')
    op.alter_column('scrape_findings', 'scan_id', new_column_name='old_scan_id')
    op.alter_column('scrape_findings', 'parent_id', new_column_name='old_parent_id')
    op.drop_constraint('scrape_scans_pkey', 'scrape_scans', type_='primary')
    op.alter_column('scrape_scans', 'id', new_column_name='old_id')

    # 3. scrape_scans on the base.
    op.execute("ALTER TABLE scrape_scans ADD COLUMN id BIGSERIAL")
    op.create_primary_key('scrape_scans_pkey', 'scrape_scans', ['id'])
    op.add_column('scrape_scans', sa.Column('external_id', sa.Uuid(), nullable=False,
                                            server_default=sa.text('gen_random_uuid()')))
    op.create_unique_constraint('uq_scrape_scans_external_id', 'scrape_scans', ['external_id'])
    op.add_column('scrape_scans', sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False,
                                            server_default=sa.text('now()')))
    op.execute("UPDATE scrape_scans SET created_at = now() WHERE created_at IS NULL")
    op.alter_column('scrape_scans', 'created_at', nullable=False)

    # 4. scrape_findings on the base, re-pointed through the old ids.
    op.execute("ALTER TABLE scrape_findings ADD COLUMN id BIGSERIAL")
    op.create_primary_key('scrape_findings_pkey', 'scrape_findings', ['id'])
    op.add_column('scrape_findings', sa.Column('external_id', sa.Uuid(), nullable=False,
                                               server_default=sa.text('gen_random_uuid()')))
    op.create_unique_constraint('uq_scrape_findings_external_id', 'scrape_findings', ['external_id'])
    op.add_column('scrape_findings', sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                                               server_default=sa.text('now()')))
    op.add_column('scrape_findings', sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False,
                                               server_default=sa.text('now()')))
    op.add_column('scrape_findings', sa.Column('scan_id', sa.BigInteger(), nullable=True))
    op.add_column('scrape_findings', sa.Column('parent_id', sa.BigInteger(), nullable=True))
    op.execute("UPDATE scrape_findings f SET scan_id = s.id FROM scrape_scans s "
               "WHERE f.old_scan_id = s.old_id")
    op.execute("UPDATE scrape_findings f SET parent_id = p.id FROM scrape_findings p "
               "WHERE f.old_parent_id = p.old_id")
    op.alter_column('scrape_findings', 'scan_id', nullable=False)
    op.create_foreign_key('scrape_findings_scan_id_fkey', 'scrape_findings', 'scrape_scans',
                          ['scan_id'], ['id'])
    op.create_foreign_key('scrape_findings_parent_id_fkey', 'scrape_findings', 'scrape_findings',
                          ['parent_id'], ['id'])
    op.create_index('ix_scrape_findings_scan_id', 'scrape_findings', ['scan_id'], unique=False)
    op.drop_column('scrape_findings', 'old_parent_id')
    op.drop_column('scrape_findings', 'old_scan_id')
    op.drop_column('scrape_findings', 'old_id')
    op.drop_column('scrape_scans', 'old_id')

    # 5. The Website warehouse tables (generated).
    op.create_table('web_tables',
    sa.Column('survey_id', sa.Integer(), nullable=False),
    sa.Column('finding_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
    sa.Column('dataset_id', sa.String(length=16), nullable=True),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('page_url', sa.Text(), nullable=False),
    sa.Column('source_locator', sa.JSON(), nullable=False),
    sa.Column('place_column', sa.Text(), nullable=True),
    sa.Column('state_name', sa.Text(), nullable=True),
    sa.Column('state_column', sa.Text(), nullable=True),
    sa.Column('year', sa.String(length=9), nullable=True),
    sa.Column('year_column', sa.Text(), nullable=True),
    sa.Column('kept_columns', sa.JSON(), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('match_summary', sa.JSON(), nullable=True),
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('external_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['finding_id'], ['scrape_findings.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('external_id')
    )
    op.create_index(op.f('ix_web_tables_survey_id'), 'web_tables', ['survey_id'], unique=False)
    op.create_table('web_master',
    sa.Column('survey_id', sa.Integer(), nullable=False),
    sa.Column('web_table_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('state_name', sa.Text(), nullable=True),
    sa.Column('state_id', sa.Integer(), nullable=True),
    sa.Column('district_name', sa.Text(), nullable=True),
    sa.Column('district_id', sa.Integer(), nullable=True),
    sa.Column('unique_id', sa.Integer(), nullable=False),
    sa.Column('year', sa.String(length=9), nullable=False),
    sa.Column('indicator', sa.Text(), nullable=False),
    sa.Column('value', sa.Float(), nullable=True),
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('external_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['web_table_id'], ['web_tables.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('external_id'),
    sa.UniqueConstraint('web_table_id', 'unique_id', 'year', 'indicator', name='uq_web_master_key')
    )
    op.create_index(op.f('ix_web_master_survey_id'), 'web_master', ['survey_id'], unique=False)
    op.create_index(op.f('ix_web_master_web_table_id'), 'web_master', ['web_table_id'], unique=False)
    op.create_table('web_raw',
    sa.Column('survey_id', sa.Integer(), nullable=False),
    sa.Column('web_table_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('row_number', sa.Integer(), nullable=False),
    sa.Column('place_text', sa.Text(), nullable=True),
    sa.Column('state_text', sa.Text(), nullable=True),
    sa.Column('year_text', sa.Text(), nullable=True),
    sa.Column('column_name', sa.Text(), nullable=False),
    sa.Column('value_text', sa.Text(), nullable=True),
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('external_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['web_table_id'], ['web_tables.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('external_id')
    )
    op.create_index(op.f('ix_web_raw_survey_id'), 'web_raw', ['survey_id'], unique=False)
    op.create_index(op.f('ix_web_raw_web_table_id'), 'web_raw', ['web_table_id'], unique=False)
    op.create_table('web_clean',
    sa.Column('survey_id', sa.Integer(), nullable=False),
    sa.Column('web_table_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('raw_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
    sa.Column('state_name', sa.Text(), nullable=True),
    sa.Column('state_id', sa.Integer(), nullable=True),
    sa.Column('district_name', sa.Text(), nullable=True),
    sa.Column('district_id', sa.Integer(), nullable=True),
    sa.Column('unique_id', sa.Integer(), nullable=True),
    sa.Column('matched', sa.Boolean(), nullable=False),
    sa.Column('year', sa.String(length=9), nullable=True),
    sa.Column('indicator', sa.Text(), nullable=False),
    sa.Column('value', sa.Float(), nullable=True),
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('external_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['raw_id'], ['web_raw.id'], ),
    sa.ForeignKeyConstraint(['web_table_id'], ['web_tables.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('external_id')
    )
    op.create_index(op.f('ix_web_clean_survey_id'), 'web_clean', ['survey_id'], unique=False)
    op.create_index(op.f('ix_web_clean_web_table_id'), 'web_clean', ['web_table_id'], unique=False)


def downgrade() -> None:
    """Drop the web tables; the scrape id conversion is one-way."""
    for table in ('web_clean', 'web_raw', 'web_master', 'web_tables'):
        op.drop_table(table)
    raise NotImplementedError("The scrape id conversion is one-way.")
