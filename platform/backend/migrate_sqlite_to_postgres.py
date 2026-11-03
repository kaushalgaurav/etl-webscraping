"""Copy everything in data/warehouse/platform.db into the PostgreSQL database
named by PLATFORM_DATABASE_URL (.env or environment).

    python migrate_sqlite_to_postgres.py            # skip tables already present
    python migrate_sqlite_to_postgres.py --replace  # drop and recopy them

Metadata tables (sources, datasets, jobs, profiles, kpi_requests) are created
from the ORM models, so their types match what the app expects; per-dataset
ds_* tables are copied with pandas.
"""
import sys

import pandas as pd
from sqlalchemy import create_engine, inspect, text

from app.core.config import DB_PATH, settings
from app.crud.dataset import pg_copy_insert, safe_column_names
from app.db.base import Base
from app import models  # noqa: F401  (registers tables on Base.metadata)

if settings.database_url.startswith("sqlite"):
    sys.exit("PLATFORM_DATABASE_URL is not set to a PostgreSQL URL (see .env.example).")

replace = "--replace" in sys.argv
src = create_engine(f"sqlite:///{DB_PATH.as_posix()}")
dst = create_engine(settings.database_url)

src_tables = inspect(src).get_table_names()
dst_existing = set(inspect(dst).get_table_names())

# Metadata tables, parents before children (FK order).
meta_tables = [t for t in Base.metadata.sorted_tables if t.name in src_tables]
if replace:
    Base.metadata.drop_all(dst, tables=meta_tables)
    dst_existing -= {t.name for t in meta_tables}
Base.metadata.create_all(dst)

for table in meta_tables:
    if table.name in dst_existing:
        print(f"skip  {table.name} (exists; use --replace)")
        continue
    with src.connect() as s_conn:
        rows = [dict(r._mapping) for r in s_conn.execute(table.select())]
    if rows:
        with dst.begin() as d_conn:
            d_conn.execute(table.insert(), rows)
    print(f"copy  {table.name}: {len(rows)} rows")

for name in sorted(t for t in src_tables if t.startswith("ds_")):
    if name in dst_existing and not replace:
        print(f"skip  {name} (exists; use --replace)")
        continue
    df = safe_column_names(pd.read_sql_table(name, src))
    with dst.begin() as d_conn:
        if replace:
            d_conn.execute(text(f'DROP TABLE IF EXISTS "{name}"'))
        df.to_sql(name, d_conn, index=False, method=pg_copy_insert)
    print(f"copy  {name}: {len(df)} rows x {df.shape[1]} cols")

print("Done.")
