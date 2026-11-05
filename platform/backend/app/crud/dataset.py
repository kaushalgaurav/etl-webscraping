"""Dataset persistence: raw files on disk, rows in SQL, metadata rows in ORM.

"RAW DATA STORAGE" = files under data/raw/<dataset_id>/.
"SQL DB" = the same engine as the metadata store (SQLite or PostgreSQL); each
dataset gets its own table named ds_<dataset_id>, written via DataFrame.to_sql.
"""
import pandas as pd
from fastapi import HTTPException
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.core.config import RAW_DIR
from app.db.session import engine
from app.models import Dataset, dataset_table_name
from app.models.common import utcnow
from app.modules.census_warehouse import schema as census_schema
from app.modules.hmis_warehouse import schema as hmis_schema
from app.modules.merging import schema as merging_schema
from app.modules.web_warehouse import schema as web_schema


# PostgreSQL truncates identifiers to 63 bytes, which can silently make two
# long column names collide. Applied on every backend so a dataset has the
# same columns whichever database stores it.
MAX_IDENTIFIER_BYTES = 63


def safe_column_names(df: pd.DataFrame) -> pd.DataFrame:
    """Stringify, truncate to 63 bytes and dedupe column names."""
    used: set[str] = set()
    cols = []
    for col in df.columns:
        base = str(col).encode("utf-8")[:MAX_IDENTIFIER_BYTES].decode("utf-8", "ignore")
        name, i = base, 1
        while name in used:
            suffix = f"_{i}"
            name = base.encode("utf-8")[: MAX_IDENTIFIER_BYTES - len(suffix)].decode("utf-8", "ignore") + suffix
            i += 1
        used.add(name)
        cols.append(name)
    return df.set_axis(cols, axis=1)


def widen_numeric_dtypes(df: pd.DataFrame) -> pd.DataFrame:
    """Cast integer measure columns to float before writing.

    A table's column types are fixed by whichever chunk creates it. pandas
    types an integer column as int64 when no value is missing and as float the
    moment one is, so a first chunk can create `state_id` as BIGINT and a later
    one then fails mid-COPY with: invalid input syntax for type bigint "10.0".
    Writing them all as float keeps one chunk from dictating a type the next
    cannot satisfy. The survey stamp is exempt — it is a real integer id and
    never missing.
    """
    out = df.copy()
    for col in out.columns:
        if col == census_schema.SURVEY_COL:
            continue
        if pd.api.types.is_integer_dtype(out[col]) and not pd.api.types.is_bool_dtype(out[col]):
            out[col] = out[col].astype("float64")
    return out


def _sql_type(series: pd.Series) -> str:
    """SQL type for a column added by ALTER TABLE.

    bool has to be tested before the numeric check: pandas counts it as
    numeric, and typing it DOUBLE PRECISION makes PostgreSQL reject the
    literal "False" on the next insert.
    """
    if pd.api.types.is_bool_dtype(series):
        return "BOOLEAN"
    # Only the survey stamp is typed as an integer. Measure columns must stay
    # DOUBLE PRECISION even when a chunk happens to hold whole numbers: pandas
    # represents an integer column with any NaN as float, so the next chunk
    # sends "10.0" and a BIGINT column rejects it mid-COPY.
    if series.name == census_schema.SURVEY_COL and pd.api.types.is_integer_dtype(series):
        return "BIGINT"
    if pd.api.types.is_numeric_dtype(series):
        return "DOUBLE PRECISION"
    return "TEXT"


def pg_copy_insert(table, conn, keys, data_iter) -> None:
    """pandas.to_sql `method` that bulk-loads with PostgreSQL COPY.

    Row-by-row or multi-row INSERTs spend most of their time building SQL in
    Python; COPY streams CSV straight into the table and is orders of
    magnitude faster for the 100k+ row Census tables.
    """
    import csv
    import io

    buf = io.StringIO()
    csv.writer(buf).writerows(data_iter)
    buf.seek(0)
    # Plain SQL identifier quoting: the dialect's quote() doubles "%" for
    # psycopg2 parameters, but copy_expert sends the statement verbatim.
    def quote(name: str) -> str:
        return '"' + str(name).replace('"', '""') + '"'

    target = f"{quote(table.schema)}.{quote(table.name)}" if table.schema else quote(table.name)
    columns = ", ".join(quote(k) for k in keys)
    with conn.connection.cursor() as cur:
        cur.copy_expert(f"COPY {target} ({columns}) FROM STDIN WITH (FORMAT csv)", buf)


def to_sql_method(conn):
    """Fastest to_sql insert method for this connection's database."""
    return pg_copy_insert if conn.dialect.name == "postgresql" else None


def save_raw_file(dataset_id: str, raw: bytes, filename: str) -> str:
    folder = RAW_DIR / dataset_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / filename
    path.write_bytes(raw)
    return str(path)


def persist_dataframe(
    db: Session,
    df: pd.DataFrame,
    *,
    name: str,
    stage: str,
    connector: str,
    source_ref: str | None,
    raw_file_path: str | None = None,
    parent_ids: list[str] | None = None,
    table_name: str | None = None,
    survey_id: int | None = None,
) -> Dataset:
    """Register a new Dataset row and write its rows to a SQL table.

    `table_name` overrides the default `ds_<id>` so several datasets can share
    one table — the dashboard layer, say, which holds a row set per survey and
    is appended to rather than replaced.
    """
    dataset = Dataset(
        name=name,
        stage=stage,
        connector=connector,
        source_ref=source_ref,
        raw_file_path=raw_file_path,
        row_count=int(df.shape[0]),
        column_count=int(df.shape[1]),
        parent_ids=parent_ids or [],
        survey_id=survey_id,
    )
    db.add(dataset)
    db.flush()  # assigns dataset.id via the default factory

    dataset.table_name = table_name or dataset_table_name(dataset.id)

    # Stringify non-primitive cells (e.g. lists from json_normalize) so
    # SQLite's type affinity doesn't choke.
    safe_df = safe_column_names(df.copy())
    for col in safe_df.columns:
        if safe_df[col].dtype == object:
            safe_df[col] = safe_df[col].apply(
                lambda v: v if v is None or isinstance(v, (str, int, float, bool)) else str(v)
            )
    # Write through the session's own connection (not a second engine
    # connection) so this shares the same transaction as the metadata row
    # above instead of contending with it for SQLite's write lock.
    if _is_shared(dataset.table_name):
        # A shared table holds several datasets, so each row has to say which
        # one wrote it — otherwise a read scoped to one dataset (`_scope`)
        # matches nothing and silently falls back to the whole table. The
        # chunked writer stamps these too; doing it here as well means a table
        # written in one go carries the same provenance as a chunked one.
        safe_df[census_schema.DATASET_COL] = dataset.id
        safe_df[census_schema.INGESTED_COL] = utcnow()
        if dataset.survey_id:
            safe_df[census_schema.SURVEY_COL] = dataset.survey_id

    conn = db.connection()
    if table_name:
        # A shared table is appended to, not replaced, or writing survey 2
        # would drop survey 1's rows along with the table. Appending also means
        # the table keeps whatever columns the first writer gave it, so a later
        # survey bringing new ones has to widen it first.
        safe_df = widen_numeric_dtypes(safe_df)
        insp = inspect(conn)
        if insp.has_table(dataset.table_name):
            existing = {c["name"] for c in insp.get_columns(dataset.table_name)}
            quote = conn.dialect.identifier_preparer.quote
            for col in safe_df.columns:
                if col not in existing:
                    conn.execute(text(
                        f"ALTER TABLE {quote(dataset.table_name)} "
                        f"ADD COLUMN {quote(col)} {_sql_type(safe_df[col])}"
                    ))
        safe_df.to_sql(dataset.table_name, conn, if_exists="append", index=False,
                       method=to_sql_method(conn))
    else:
        safe_df.to_sql(dataset.table_name, conn, if_exists="replace", index=False,
                       method=to_sql_method(conn))

    db.add(dataset)
    db.commit()
    db.refresh(dataset)
    return dataset



def clear_survey(db: Session, table_name: str, survey_id: int) -> int:
    """Delete a survey's rows from a shared layer, and retire its old datasets.

    Re-running a survey replaces it rather than stacking a second copy beside
    the first. Scoped to `survey_id`, so other surveys sharing the table are
    untouched. Returns the number of rows removed.
    """
    conn = db.connection()
    if not inspect(conn).has_table(table_name):
        return 0
    if census_schema.SURVEY_COL not in {c["name"] for c in inspect(conn).get_columns(table_name)}:
        return 0  # nothing was ever written with a survey stamp

    quote = conn.dialect.identifier_preparer.quote
    removed = conn.execute(
        text(f"DELETE FROM {quote(table_name)} WHERE {quote(census_schema.SURVEY_COL)} = :s"),
        {"s": survey_id},
    ).rowcount
    # The old Dataset rows now describe nothing; drop them so row counts and
    # lineage do not double-count a survey that has been replaced.
    stale = [d.id for d in db.query(Dataset.id).filter(
        Dataset.survey_id == survey_id, Dataset.table_name == table_name).all()]
    if stale:
        db.query(Dataset).filter(Dataset.id.in_(stale)).delete(synchronize_session=False)
    db.commit()
    return int(removed or 0)


def create_dataset_shell(
    db: Session,
    *,
    name: str,
    stage: str,
    connector: str,
    source_ref: str | None,
    table_name: str | None = None,
    survey_id: int | None = None,
) -> Dataset:
    """Register an empty Dataset whose table is filled later by append_dataframe().

    Used for chunked ingests too large to hold in memory as one DataFrame.
    `table_name` overrides the default `ds_<id>` so several datasets can share
    one table (see app/modules/census_warehouse); their rows stay separable
    because append_dataframe stamps each with the dataset id.
    """
    dataset = Dataset(
        name=name, stage=stage, connector=connector, source_ref=source_ref,
        row_count=0, column_count=0, parent_ids=[], survey_id=survey_id,
    )
    db.add(dataset)
    db.flush()
    dataset.table_name = table_name or dataset_table_name(dataset.id)
    db.commit()
    db.refresh(dataset)
    return dataset


def append_dataframe(db: Session, dataset: Dataset, df: pd.DataFrame) -> None:
    """Append rows to a dataset's table, adding any columns it doesn't have yet.

    Chunks can differ in columns (an indicator absent in one district), so new
    columns are added with ALTER TABLE; columns missing from a chunk are left
    NULL for its rows.
    """
    df = safe_column_names(df)
    shared = _is_shared(dataset.table_name)
    if shared:
        # A shared table holds several datasets, so each row has to say which
        # one it came from; without this the dataset could not be read back.
        df = df.copy()
        df[census_schema.DATASET_COL] = dataset.id
        df[census_schema.INGESTED_COL] = utcnow()
        # Stamp the survey too, so a re-run can replace exactly its own rows
        # and leave every other survey in the layer alone.
        if dataset.survey_id:
            df[census_schema.SURVEY_COL] = dataset.survey_id

    conn = db.connection()
    insp = inspect(conn)
    if insp.has_table(dataset.table_name):
        existing = {c["name"] for c in insp.get_columns(dataset.table_name)}
        quote = conn.dialect.identifier_preparer.quote
        for col in df.columns:
            if col not in existing:
                sql_type = _sql_type(df[col])
                conn.execute(text(
                    f"ALTER TABLE {quote(dataset.table_name)} ADD COLUMN {quote(col)} {sql_type}"
                ))
                existing.add(col)
        # For a shared table `existing` counts every other dataset's columns
        # too, so report only the ones this dataset actually writes.
        column_count = df.shape[1] if shared else len(existing)
    else:
        column_count = df.shape[1]

    df.to_sql(dataset.table_name, conn, if_exists="append", index=False,
              method=to_sql_method(conn))
    dataset.row_count += int(df.shape[0])
    dataset.column_count = column_count
    db.add(dataset)
    db.commit()


def _scope(dataset: Dataset) -> tuple[str, dict]:
    """WHERE clause and params restricting a read to this dataset's own rows.

    Empty for a `ds_<id>` table, which holds nothing else. For a shared census
    layer table it filters on the stamped dataset id, so every caller that goes
    through load_dataframe/load_page keeps seeing one dataset.
    """
    if web_schema.is_layer_table(dataset.table_name):
        # A Website record card: its rows are those of the web_tables row it
        # belongs to (the web layers carry no _dataset_id).
        return (f" WHERE web_table_id IN (SELECT id FROM {web_schema.TABLES} "
                "WHERE dataset_id = :_ds)", {"_ds": dataset.id})
    if not _is_shared(dataset.table_name):
        return "", {}
    quote = engine.dialect.identifier_preparer.quote
    return f" WHERE {quote(census_schema.DATASET_COL)} = :_ds", {"_ds": dataset.id}


def _is_shared(table_name: str | None) -> bool:
    """True for any warehouse layer table shared by several datasets.

    dashboard_master belongs here too: every merge and re-clean appends to the
    one table, so without the dataset stamp a read scoped to one merge run
    returned the whole table — every survey's rows at once.
    """
    return (
        census_schema.is_layer_table(table_name)
        or hmis_schema.is_layer_table(table_name)
        or merging_schema.is_layer_table(table_name)
    )


def _hide_bookkeeping(df: pd.DataFrame) -> pd.DataFrame:
    """Drop the shared table's provenance columns.

    They exist so one table can hold several datasets; once a read is already
    scoped to one, they are noise. `level` is kept — it is real data about the
    row, not bookkeeping.
    """
    return df.drop(
        columns=[census_schema.DATASET_COL, census_schema.INGESTED_COL], errors="ignore"
    )


def load_dataframe(dataset: Dataset) -> pd.DataFrame:
    if not dataset.table_name:
        raise ValueError(f"Dataset {dataset.id} has no backing table.")
    where, params = _scope(dataset)
    if not where:
        return pd.read_sql_table(dataset.table_name, engine)
    table = engine.dialect.identifier_preparer.quote(dataset.table_name)
    with engine.connect() as conn:
        df = pd.read_sql_query(text(f"SELECT * FROM {table}{where}"), conn, params=params)
    return _hide_bookkeeping(df)


def load_page(dataset: Dataset, limit: int, offset: int) -> tuple[int, pd.DataFrame]:
    """(total rows, one page) read with LIMIT/OFFSET, so large tables stay on disk."""
    if not dataset.table_name:
        raise ValueError(f"Dataset {dataset.id} has no backing table.")
    table = engine.dialect.identifier_preparer.quote(dataset.table_name)
    where, params = _scope(dataset)
    with engine.connect() as conn:
        total = conn.execute(text(f"SELECT COUNT(*) FROM {table}{where}"), params).scalar_one()
        page = pd.read_sql_query(
            text(f"SELECT * FROM {table}{where} LIMIT :limit OFFSET :offset"),
            conn, params={**params, "limit": limit, "offset": offset},
        )
    return int(total), _hide_bookkeeping(page)


def get_dataset_or_404(db: Session, dataset_id: str) -> Dataset:
    dataset = db.get(Dataset, dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail=f"Dataset '{dataset_id}' not found.")
    return dataset
