"""Builds the census warehouse: backfill into raw, then clean, then master.

raw -> clean is the ordinary cleaning pass, run over the whole table at once
rather than per dataset. clean -> master is the step that makes the answer
*correct*: the same district has been fetched many times, so master keeps one
row per area, the most recently ingested one.
"""
from pathlib import Path

import pandas as pd
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.core.config import EXPORTS_DIR
from app.crud import warehouse_survey as survey
from app.constants import district_crosswalk as crosswalk
from app.crud.dataset import widen_numeric_dtypes, _sql_type, safe_column_names, to_sql_method
from app.helpers.dataframe import snake_case
from app.models import Dataset
from app.modules.census_warehouse import schema as S
from app.modules.cleaning import service as cleaning


def _level_of(dataset: Dataset, columns: set[str]) -> str:
    """Infer a dataset's census level from the geography columns it carries.

    Older datasets predate the `level` column, and the name is free text, so
    the columns are the only reliable signal.
    """
    if "town_village" in columns:
        return "village_town"
    if "subdistrict" in columns:
        return "subdistrict"
    return "district"


def backfill(db: Session, survey_id: int | None = None) -> dict:
    """Copy every existing census `ds_<id>` table into census_raw.

    Column names are snake_cased on the way in: some early datasets stored the
    API's own spelling ("Total population - Both sexes"), which is the same
    column under a different name and must not become a second one.

    `survey_id` limits the copy to that survey's datasets.
    """
    conn = db.connection()
    insp = inspect(conn)
    query = db.query(Dataset).filter(Dataset.connector == "census_india",
                                     Dataset.stage != "transformed")
    if survey_id is not None:
        query = query.filter(Dataset.survey_id == survey_id)
    datasets = query.order_by(Dataset.created_at).all()

    # Re-running must not double-load. Only rows previously copied by a
    # backfill are cleared; rows a fetch wrote straight into census_raw belong
    # to datasets that aren't candidates here, so they are left alone.
    candidates = [d.id for d in datasets if d.table_name and d.table_name not in S.LAYERS]
    if candidates and insp.has_table(S.RAW):
        quoted = conn.dialect.identifier_preparer.quote(S.RAW)
        col = conn.dialect.identifier_preparer.quote(S.DATASET_COL)
        conn.execute(text(f"DELETE FROM {quoted} WHERE {col} = ANY(:ids)"), {"ids": candidates})

    copied, skipped = [], []
    for ds in datasets:
        table = ds.table_name
        if not table or table in S.LAYERS or not insp.has_table(table):
            skipped.append({"dataset_id": ds.id, "reason": "no ds_ table"})
            continue
        quoted = conn.dialect.identifier_preparer.quote(table)
        df = pd.read_sql_query(text(f"SELECT * FROM {quoted}"), conn)
        if df.empty:
            skipped.append({"dataset_id": ds.id, "reason": "empty"})
            continue

        df = df.rename(columns={c: snake_case(c) for c in df.columns})
        df = df.loc[:, ~df.columns.duplicated()]
        df[S.LEVEL_COL] = _level_of(ds, set(df.columns))
        df[S.DATASET_COL] = ds.id
        df[S.INGESTED_COL] = ds.created_at
        survey.stamp(df, ds.survey_id)

        _append(conn, S.RAW, df)
        copied.append({"dataset_id": ds.id, "level": df[S.LEVEL_COL].iloc[0],
                       "rows": int(df.shape[0]), "name": ds.name})

    db.commit()
    return {
        "table": S.RAW,
        "datasets_copied": len(copied),
        "rows": sum(c["rows"] for c in copied),
        "copied": copied,
        "skipped": skipped,
    }


def _append(conn, table: str, df: pd.DataFrame) -> None:
    """to_sql(append) that widens the table when a chunk brings new columns."""
    df = widen_numeric_dtypes(safe_column_names(df))
    insp = inspect(conn)
    if insp.has_table(table):
        existing = {c["name"] for c in insp.get_columns(table)}
        quote = conn.dialect.identifier_preparer.quote
        for col in df.columns:
            if col not in existing:
                sql_type = _sql_type(df[col])
                conn.execute(text(f"ALTER TABLE {quote(table)} ADD COLUMN {quote(col)} {sql_type}"))
                existing.add(col)
    df.to_sql(table, conn, if_exists="append", index=False, method=to_sql_method(conn))


def _ordered(df: pd.DataFrame) -> pd.DataFrame:
    """Meta, then geography coarse-to-fine, then indicators alphabetically."""
    lead = [c for c in (*S.META_COLUMNS, *S.GEO_COLUMNS) if c in df.columns]
    rest = sorted(c for c in df.columns if c not in lead)
    return df[lead + rest]


def _resolve_geography(df: pd.DataFrame) -> pd.DataFrame:
    """Add the crosswalk's state/district ids to census rows.

    The census API names places in its own way and numbers them with its own
    codes, while HMIS names them differently again. Both are resolved through
    app/constants/district_crosswalk.tsv so the two warehouses carry the same
    `state_id` / `district_id` / `unique_id`, which is what lets a survey's
    census and HMIS rows be joined — names and source codes never match.

    A place the crosswalk does not know keeps null ids rather than a guess.
    """
    if "state_name" not in df.columns:
        return df

    has_district = "district_name" in df.columns
    pairs = df[["state_name"] + (["district_name"] if has_district else [])].drop_duplicates()

    resolved = []
    for row in pairs.itertuples(index=False):
        state_name = getattr(row, "state_name", None)
        district_name = getattr(row, "district_name", None) if has_district else None
        state = crosswalk.lookup_state(state_name or "")
        district = (crosswalk.lookup_district(state_name or "", district_name or "")
                    if district_name else None)
        entry = {
            "state_name": state_name,
            "state_id": (state or {}).get("state_id"),
            "district_id": (district or {}).get("district_id"),
            "unique_id": (district or {}).get("unique_id"),
        }
        if has_district:
            entry["district_name"] = district_name
        resolved.append(entry)

    on = ["state_name"] + (["district_name"] if has_district else [])
    return df.merge(pd.DataFrame(resolved), on=on, how="left")


def build_clean(db: Session, operations: list[str] | None = None,
                survey_id: int | None = None) -> dict:
    """census_raw -> census_clean, via the standard cleaning operations.

    With `survey_id`, only that survey's rows are rebuilt; every other
    survey's rows in census_clean are left as they are.

    Cleaned one dataset at a time, not as one table. The cleaning operations
    drop and reorder rows, so a column set aside beforehand cannot be put back
    against the result — and getting that wrong silently attaches one fetch's
    `level` to another's rows. Within a single dataset the meta values are
    constant, so they can be restamped afterwards with nothing to misalign.
    Cleaning per dataset also keeps `drop_duplicates` meaning "duplicated
    within this fetch"; identical rows from two fetches are real provenance and
    are resolved by the master layer instead.
    """
    conn = db.connection()
    if not inspect(conn).has_table(S.RAW):
        raise ValueError(f"{S.RAW} does not exist yet — run the backfill first.")

    quoted = conn.dialect.identifier_preparer.quote(S.RAW)
    clause, params = survey.where(conn, S.RAW, survey_id)
    df = pd.read_sql_query(text(f"SELECT * FROM {quoted}{clause}"), conn, params=params)
    if df.empty:
        raise ValueError(f"{S.RAW} is empty." if survey_id is None
                         else f"{S.RAW} has no rows for survey {survey_id}.")

    parts: list[pd.DataFrame] = []
    report: list[str] = []
    for (dataset_id, level), group in df.groupby([S.DATASET_COL, S.LEVEL_COL], dropna=False):
        ingested = group[S.INGESTED_COL].iloc[0]
        survey_of = group[S.SURVEY_COL].iloc[0] if S.SURVEY_COL in group else None
        cleaned, notes = cleaning.clean_dataframe(
            group.drop(columns=list(S.META_COLUMNS), errors="ignore"),
            operations or list(cleaning.WAREHOUSE_OPERATIONS),
        )
        cleaned = _resolve_geography(cleaned)
        cleaned[S.DATASET_COL] = dataset_id
        cleaned[S.LEVEL_COL] = level
        cleaned[S.INGESTED_COL] = ingested
        survey.stamp(cleaned, survey_of)
        parts.append(cleaned)
        report += [f"{dataset_id}: {n}" for n in notes if not n.startswith("No cleaning")]

    out = pd.concat(parts, ignore_index=True)
    if survey_id is None:
        _replace(conn, S.CLEAN, _ordered(out))
    else:
        survey.clear(conn, S.CLEAN, survey_id)
        _append(conn, S.CLEAN, _ordered(out))
    db.commit()
    return {"table": S.CLEAN, "survey_id": survey_id, "rows": int(out.shape[0]),
            "columns": int(out.shape[1]), "rows_in": int(df.shape[0]),
            "operations": report}


def build_master(db: Session, survey_id: int | None = None) -> dict:
    """census_clean -> census_master: one row per area per survey, most recent ingest wins.

    This is where the six separate All-India pulls collapse back into 640
    districts. Ordering by ingest time and keeping the last occurrence means a
    re-fetch corrects an earlier one instead of duplicating it. `survey_id` is
    part of the key, so two surveys never overwrite each other's rows — which
    is also what lets `survey_id` rebuild one survey alone.
    """
    conn = db.connection()
    if not inspect(conn).has_table(S.CLEAN):
        raise ValueError(f"{S.CLEAN} does not exist yet — build the clean layer first.")

    quoted = conn.dialect.identifier_preparer.quote(S.CLEAN)
    clause, params = survey.where(conn, S.CLEAN, survey_id)
    df = pd.read_sql_query(text(f"SELECT * FROM {quoted}{clause}"), conn, params=params)
    if df.empty:
        raise ValueError(f"{S.CLEAN} is empty." if survey_id is None
                         else f"{S.CLEAN} has no rows for survey {survey_id}.")

    if S.SURVEY_COL in df.columns:
        # NULL surveys read back as float; keep the column an integer.
        df[S.SURVEY_COL] = df[S.SURVEY_COL].astype("Int64")
    key = [c for c in (S.SURVEY_COL, *S.MASTER_KEY) if c in df.columns]
    before = int(df.shape[0])
    df = df.sort_values(S.INGESTED_COL).drop_duplicates(subset=key, keep="last")
    df = df.sort_values(key).reset_index(drop=True)

    if survey_id is None:
        _replace(conn, S.MASTER, _ordered(df))
    else:
        survey.clear(conn, S.MASTER, survey_id)
        _append(conn, S.MASTER, _ordered(df))
    db.commit()
    per_level = df.groupby(S.LEVEL_COL).size().to_dict()
    return {
        "table": S.MASTER,
        "survey_id": survey_id,
        "key": key,
        "rows_in": before,
        "rows": int(df.shape[0]),
        "duplicates_removed": before - int(df.shape[0]),
        "rows_by_level": {k: int(v) for k, v in per_level.items()},
    }


def _replace(conn, table: str, df: pd.DataFrame) -> None:
    df = widen_numeric_dtypes(safe_column_names(df))
    df.to_sql(table, conn, if_exists="replace", index=False, method=to_sql_method(conn))


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in str(text).lower()).strip("_")


def export_layer(db: Session, layer: str, out_dir: Path | None = None,
                 dataset_ids: list[str] | None = None,
                 survey_id: int | None = None) -> dict:
    """Write one warehouse layer to CSV — a file per level, village/town per state.

    `survey_id` exports only that survey's rows.

    Named like fetch_census_by_state.py's output rather than the per-dataset
    `api__…__<id>.csv`: these files come from shared tables, so no single
    dataset id describes them, and the name has to stay the same across
    re-fetches. Each layer gets its own directory — the names repeat across
    layers, and they also collide with that script's files.

    raw and clean keep `_dataset_id` / `_ingested_at`: they hold several pulls
    of the same area, so without them the duplicate rows cannot be told apart.
    master is already one row per area, so it drops them.

    utf-8-sig, because Excel reads a plain UTF-8 CSV as mojibake.
    """
    if layer not in S.LAYERS:
        raise ValueError(f"Unknown layer '{layer}'. Use one of: {', '.join(S.LAYERS)}")
    conn = db.connection()
    if not inspect(conn).has_table(layer):
        raise ValueError(f"{layer} does not exist yet — build it first.")

    out = Path(out_dir) if out_dir else EXPORTS_DIR / layer
    out.mkdir(parents=True, exist_ok=True)
    quoted = conn.dialect.identifier_preparer.quote(layer)

    # Scope to one job's datasets when asked, so an export need not read a
    # multi-million-row layer to write the rows a single fetch produced.
    clause, params = survey.where(conn, layer, survey_id)
    sql = f"SELECT * FROM {quoted}{clause}"
    if dataset_ids:
        placeholders = ", ".join(f":d{i}" for i in range(len(dataset_ids)))
        sql += (" AND " if clause else " WHERE ") + \
            f"{conn.dialect.identifier_preparer.quote(S.DATASET_COL)} IN ({placeholders})"
        params |= {f"d{i}": value for i, value in enumerate(dataset_ids)}

    df = pd.read_sql_query(text(sql), conn, params=params)
    if df.empty:
        raise ValueError(
            f"{layer} is empty."
            if not dataset_ids else
            f"{layer} holds no rows for dataset(s) {', '.join(dataset_ids)} — "
            "the job may have written to a different layer, or not finished."
        )
    if layer == S.MASTER:
        df = df.drop(columns=[S.DATASET_COL, S.INGESTED_COL], errors="ignore")

    written = []

    def _write(frame: pd.DataFrame, name: str) -> None:
        # Every column is kept, even all-empty ones: dropping them makes files
        # from different levels or states disagree on their columns, which
        # breaks anything that stacks them or expects a fixed header.
        path = out / name
        path.write_bytes(frame.to_csv(index=False).encode("utf-8-sig"))
        written.append({"file": name, "rows": int(frame.shape[0]),
                        "columns": int(frame.shape[1]), "bytes": path.stat().st_size})

    for level, group in df.groupby(S.LEVEL_COL):
        # `level` is kept rather than dropped as redundant with the filename:
        # it makes each file self-describing, and it is needed the moment two
        # levels are stacked back together.
        # A national village/town file would be ~640k rows; per state keeps each
        # one openable, and matches the existing census_by_state layout.
        if level == "village_town":
            for state, part in group.groupby("state"):
                name = part["state_name"].dropna().iloc[0] if part["state_name"].notna().any() else state
                _write(part, f"census2011_{level}_{int(state):02d}_{_slug(name)}.csv")
        else:
            _write(group, f"census2011_{level}.csv")

    return {"layer": layer, "directory": str(out), "files": len(written),
            "rows": sum(w["rows"] for w in written), "written": written}


def export_all(db: Session, layers: list[str] | None = None,
               dataset_ids: list[str] | None = None,
               survey_id: int | None = None) -> dict:
    """Export several layers, each into its own directory.

    `dataset_ids` narrows every layer to those datasets — used to export just
    what one job fetched. `survey_id` narrows it to one survey.
    """
    return {l: export_layer(db, l, dataset_ids=dataset_ids, survey_id=survey_id)
            for l in (layers or list(S.LAYERS))}


def summary(db: Session, survey_id: int | None = None) -> dict:
    """Row counts and per-level breakdown for whichever layers exist.

    With `survey_id`, counts only that survey's rows.
    """
    conn = db.connection()
    insp = inspect(conn)
    out: dict = {}
    for table in S.LAYERS:
        if not insp.has_table(table):
            out[table] = None
            continue
        quoted = conn.dialect.identifier_preparer.quote(table)
        try:
            clause, params = survey.where(conn, table, survey_id)
        except ValueError as exc:
            out[table] = {"error": str(exc)}
            continue
        rows = conn.execute(text(f"SELECT COUNT(*) FROM {quoted}{clause}"), params).scalar_one()
        levels = dict(conn.execute(text(
            f"SELECT {S.LEVEL_COL}, COUNT(*) FROM {quoted}{clause} GROUP BY 1 ORDER BY 1"
        ), params).all())
        out[table] = {
            "survey_id": survey_id,
            "rows": int(rows),
            "columns": len(insp.get_columns(table)),
            "rows_by_level": {k: int(v) for k, v in levels.items()},
        }
    return out
