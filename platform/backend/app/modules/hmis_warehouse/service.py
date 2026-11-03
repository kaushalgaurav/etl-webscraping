"""Builds the HMIS warehouse: raw -> clean -> master.

raw holds every report exactly as parsed, one row per (area, indicator,
metric year), tagged with the dataset that produced it. clean types and
standardises. master resolves the duplication that is inherent to these
reports: each year's file repeats the previous year's figures for comparison,
so the same (area, indicator, metric year) arrives from two different report
years and only the most recent publication should survive.
"""
from pathlib import Path

import pandas as pd
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.constants import district_crosswalk as crosswalk
from app.core.config import EXPORTS_DIR
from app.crud import warehouse_survey as survey
from app.crud.dataset import widen_numeric_dtypes, _sql_type, safe_column_names, to_sql_method
from app.helpers.dataframe import snake_case
from app.models import Dataset
from app.modules.cleaning import service as cleaning
from app.modules.hmis_warehouse import schema as S


def backfill(db: Session, survey_id: int | None = None) -> dict:
    """Copy every HMIS dataset still held in its own `ds_<id>` table into hmis_raw.

    New crawls write to hmis_raw directly, so this only matters for datasets
    ingested before the warehouse existed. Re-running is safe: rows a previous
    backfill copied for those datasets are cleared first. `survey_id` limits
    the copy to that survey's datasets.
    """
    conn = db.connection()
    insp = inspect(conn)
    query = db.query(Dataset).filter(Dataset.connector == "hmis_reports",
                                     Dataset.stage != "transformed")
    if survey_id is not None:
        query = query.filter(Dataset.survey_id == survey_id)
    datasets = query.order_by(Dataset.created_at).all()

    candidates = [d.id for d in datasets if d.table_name and d.table_name not in S.LAYERS]
    if candidates and insp.has_table(S.RAW):
        quoted = conn.dialect.identifier_preparer.quote(S.RAW)
        col = conn.dialect.identifier_preparer.quote(S.DATASET_COL)
        conn.execute(text(f"DELETE FROM {quoted} WHERE {col} = ANY(:ids)"), {"ids": candidates})

    copied, skipped = [], []
    for ds in datasets:
        table = ds.table_name
        if not table or table in S.LAYERS or not insp.has_table(table):
            skipped.append({"dataset_id": ds.id, "reason": "already in warehouse or no ds_ table"})
            continue
        quoted = conn.dialect.identifier_preparer.quote(table)
        df = pd.read_sql_query(text(f"SELECT * FROM {quoted}"), conn)
        if df.empty:
            skipped.append({"dataset_id": ds.id, "reason": "empty"})
            continue

        df = df.rename(columns={c: snake_case(c) for c in df.columns})
        df = df.loc[:, ~df.columns.duplicated()]
        df[S.DATASET_COL] = ds.id
        df[S.INGESTED_COL] = ds.created_at
        survey.stamp(df, ds.survey_id)

        _append(conn, S.RAW, _ordered(df))
        copied.append({"dataset_id": ds.id, "rows": int(df.shape[0]), "name": ds.name})

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
                conn.execute(text(
                    f"ALTER TABLE {quote(table)} ADD COLUMN {quote(col)} {_sql_type(df[col])}"
                ))
                existing.add(col)
    df.to_sql(table, conn, if_exists="append", index=False, method=to_sql_method(conn))


def _replace(conn, table: str, df: pd.DataFrame) -> None:
    df = widen_numeric_dtypes(safe_column_names(df))
    df.to_sql(table, conn, if_exists="replace", index=False, method=to_sql_method(conn))


def _ordered(df: pd.DataFrame) -> pd.DataFrame:
    """Meta, then provenance, then geography, then indicator, then value."""
    lead = [c for c in (*S.META_COLUMNS, *S.SOURCE_COLUMNS, *S.GEO_COLUMNS,
                        *S.GEO_ID_COLUMNS, *S.INDICATOR_COLUMNS,
                        *S.VALUE_COLUMNS) if c in df.columns]
    return df[lead + sorted(c for c in df.columns if c not in lead)]


def _resolve_geography(df: pd.DataFrame) -> pd.DataFrame:
    """Add state/district names and ids from the district master.

    The reports carry names only, and every source spells them differently,
    so the ids come from app/constants/district_crosswalk.tsv. Both are kept:
    the name because it is what the report actually said, the id because it
    is what joins to the census and to PLFS. A district the master does not
    know is left with null ids rather than guessed at.
    """
    pairs = df[["level", "parent_area", "area"]].drop_duplicates()
    resolved = []
    for level, parent, area in pairs.itertuples(index=False):
        # "national" is the All India total and belongs to no state. A "state"
        # row is either a state listed in the national file, or a state's own
        # total row inside its folder ("_Bihar") — in both cases the state is
        # the row itself, stripped of the underscore. Only a "district" row
        # takes its state from the folder.
        if level == "national":
            state_name, district_name = None, None
        elif level == "state":
            state_name, district_name = (area or "").lstrip("_"), None
        else:
            state_name, district_name = parent, area
        state = crosswalk.lookup_state(state_name or "")
        district = (crosswalk.lookup_district(state_name or "", district_name or "")
                    if district_name else None)
        resolved.append({
            "level": level, "parent_area": parent, "area": area,
            "state_name": (state or {}).get("state") or crosswalk.canonical_state(state_name or ""),
            "state_id": (state or {}).get("state_id"),
            "district_name": (district or {}).get("district") or district_name,
            "district_id": (district or {}).get("district_id"),
            "unique_id": (district or {}).get("unique_id"),
        })
    lookup = pd.DataFrame(resolved)
    return df.merge(lookup, on=["level", "parent_area", "area"], how="left")


def build_clean(db: Session, operations: list[str] | None = None,
                survey_id: int | None = None) -> dict:
    """hmis_raw -> hmis_clean, one dataset at a time, streamed.

    With `survey_id`, only that survey's rows are rebuilt; every other
    survey's rows in hmis_clean are left as they are.

    Cleaned per dataset for the same reason as the census layer: the cleaning
    operations drop and reorder rows, so metadata set aside beforehand cannot
    be reattached to the result. Within one dataset those values are constant.

    Each dataset is read, cleaned and written before the next is loaded. The
    raw layer is ~12M rows; holding them all as one frame, plus the cleaned
    copy, needs tens of GB and simply fails.
    """
    conn = db.connection()
    if not inspect(conn).has_table(S.RAW):
        raise ValueError(f"{S.RAW} does not exist yet — ingest something first.")

    raw = conn.dialect.identifier_preparer.quote(S.RAW)
    clause, params = survey.where(conn, S.RAW, survey_id)
    ids = [r[0] for r in conn.execute(
        text(f"SELECT DISTINCT {S.DATASET_COL} FROM {raw}{clause} ORDER BY 1"), params
    )]
    if not ids:
        raise ValueError(f"{S.RAW} is empty." if survey_id is None
                         else f"{S.RAW} has no rows for survey {survey_id}.")

    if survey_id is None:
        conn.execute(text(f"DROP TABLE IF EXISTS {conn.dialect.identifier_preparer.quote(S.CLEAN)}"))
    else:
        survey.clear(conn, S.CLEAN, survey_id)
    report: list[str] = []
    rows_in = rows_out = 0
    for dataset_id in ids:
        group = pd.read_sql_query(
            text(f"SELECT * FROM {raw} WHERE {S.DATASET_COL} = :d"), conn, params={"d": dataset_id}
        )
        if group.empty:
            continue
        rows_in += int(group.shape[0])
        ingested = group[S.INGESTED_COL].iloc[0]
        survey_of = group[S.SURVEY_COL].iloc[0] if S.SURVEY_COL in group else None
        group = _resolve_geography(group)
        cleaned, notes = cleaning.clean_dataframe(
            group.drop(columns=list(S.META_COLUMNS), errors="ignore"),
            operations or list(cleaning.WAREHOUSE_OPERATIONS),
        )
        cleaned[S.DATASET_COL] = dataset_id
        cleaned[S.INGESTED_COL] = ingested
        survey.stamp(cleaned, survey_of)
        _append(conn, S.CLEAN, _ordered(cleaned))
        rows_out += int(cleaned.shape[0])
        report += [f"{dataset_id}: {n}" for n in notes if not n.startswith("No cleaning")]
        # Committed per dataset, not once at the end: a single transaction over
        # all of them keeps ~12M rows uncommitted for twenty minutes, during
        # which SELECT count(*) shows nothing and the progress is unwatchable.
        db.commit()
        conn = db.connection()  # commit() releases the previous handle

    cols = len(inspect(conn).get_columns(S.CLEAN))
    return {"table": S.CLEAN, "survey_id": survey_id, "datasets": len(ids), "rows_in": rows_in,
            "rows": rows_out, "columns": cols, "operations": report[:20]}


def build_master(db: Session, survey_id: int | None = None) -> dict:
    """hmis_clean -> hmis_master: one row per place per survey, most recent ingest wins.

    `survey_id` is part of the key, so surveys never overwrite each other's
    rows; passing one rebuilds only that survey's rows in hmis_master.

    Shaped to match census_master, so both warehouses answer `SELECT *` the
    same way: a row is a place and a column is a measure.

    This used to melt the clean layer and pivot it back. That step existed
    only because raw stored one row per indicator; raw now stores one row per
    record, so clean is already wide and the round trip would rebuild a shape
    it already has. All that remains is deduplication — the same ordering rule
    as census: sort by ingest time and keep the last, so a re-fetch corrects
    an earlier one instead of duplicating it.
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

    # WIDE_INDEX identifies a place and its reporting window. Columns absent
    # from this build (a layer without resolved ids, say) are skipped rather
    # than failing the key.
    key = [c for c in S.WIDE_INDEX if c in df.columns]
    if key and S.SURVEY_COL in df.columns:
        # NULL surveys read back as float; keep the column an integer.
        df[S.SURVEY_COL] = df[S.SURVEY_COL].astype("Int64")
        key = [S.SURVEY_COL, *key]
    if not key:
        raise ValueError(f"{S.CLEAN} has none of the key columns {S.WIDE_INDEX}.")

    before = int(df.shape[0])
    if S.INGESTED_COL in df.columns:
        df = df.sort_values(S.INGESTED_COL)
    df = df.drop_duplicates(subset=key, keep="last").sort_values(key).reset_index(drop=True)

    if survey_id is None:
        _replace(conn, S.MASTER, _ordered(df))
    else:
        survey.clear(conn, S.MASTER, survey_id)
        _append(conn, S.MASTER, _ordered(df))
    db.commit()

    by_level = df.groupby("level").size().to_dict() if "level" in df.columns else {}
    return {
        "table": S.MASTER,
        "survey_id": survey_id,
        "key": key,
        "rows_in": before,
        "rows": int(df.shape[0]),
        "columns": int(df.shape[1]),
        "duplicates_removed": before - int(df.shape[0]),
        "rows_by_level": {k: int(v) for k, v in by_level.items()},
    }




def summary(db: Session, survey_id: int | None = None) -> dict:
    """Row counts and breakdowns for whichever layers exist.

    With `survey_id`, counts only that survey's rows — exactly, since one
    survey's slice is far smaller than the planner-estimated whole layer.
    """
    if survey_id is not None:
        return _survey_summary(db, survey_id)
    conn = db.connection()
    insp = inspect(conn)
    out: dict = {}
    for table in S.LAYERS:
        if not insp.has_table(table):
            out[table] = None
            continue
        quoted = conn.dialect.identifier_preparer.quote(table)
        cols = {c["name"] for c in insp.get_columns(table)}
        # The long layers run to millions of rows, where an exact COUNT(*) is a
        # full scan and a COUNT(DISTINCT) worse — a summary endpoint that takes
        # minutes is not a summary. Only the wide master, which is small, is
        # counted exactly; the rest use the planner's estimate.
        estimate = int(conn.execute(
            text("SELECT reltuples::bigint FROM pg_class WHERE relname = :t"), {"t": table}
        ).scalar_one() or 0)
        # reltuples is -1 for a table never analyzed (e.g. just rebuilt), so
        # an unknown size is counted exactly rather than reported as -1.
        exact = table == S.MASTER or estimate < 200_000
        entry = {
            "columns": len(cols),
            "rows": int(conn.execute(text(f"SELECT COUNT(*) FROM {quoted}")).scalar_one())
            if exact else estimate,
            "rows_are_estimated": not exact,
        }
        if exact:
            for dim in ("level", "report_year", "variable"):
                if dim in cols:
                    entry[f"distinct_{dim}"] = int(conn.execute(
                        text(f"SELECT COUNT(DISTINCT {conn.dialect.identifier_preparer.quote(dim)}) FROM {quoted}")
                    ).scalar_one())
        out[table] = entry

    # build_clean commits per dataset, so an interrupted run leaves a
    # part-filled hmis_clean rather than none at all. Comparing the datasets
    # it covers against raw's is what tells them apart — a row count cannot.
    if out.get(S.RAW) and out.get(S.CLEAN):
        # A loose index scan over the distinct ids, not COUNT(DISTINCT): there
        # are twelve of them among twelve million rows, and the recursive form
        # walks the index instead of the table.
        def dataset_ids(table: str) -> int:
            q = conn.dialect.identifier_preparer.quote(table)
            col = conn.dialect.identifier_preparer.quote(S.DATASET_COL)
            return int(conn.execute(text(
                f"WITH RECURSIVE ids AS ("
                f"  SELECT MIN({col}) AS v FROM {q}"
                f"  UNION ALL"
                f"  SELECT (SELECT MIN({col}) FROM {q} WHERE {col} > ids.v) FROM ids"
                f"  WHERE ids.v IS NOT NULL)"
                f"SELECT COUNT(v) FROM ids"
            )).scalar_one())

        in_raw, in_clean = dataset_ids(S.RAW), dataset_ids(S.CLEAN)
        out[S.CLEAN]["datasets"] = in_clean
        out[S.CLEAN]["datasets_in_raw"] = in_raw
        out[S.CLEAN]["complete"] = in_clean == in_raw
    return out


def _survey_summary(db: Session, survey_id: int) -> dict:
    conn = db.connection()
    insp = inspect(conn)
    quote = conn.dialect.identifier_preparer.quote
    out: dict = {}
    for table in S.LAYERS:
        if not insp.has_table(table):
            out[table] = None
            continue
        try:
            clause, params = survey.where(conn, table, survey_id)
        except ValueError as exc:
            out[table] = {"error": str(exc)}
            continue
        quoted = quote(table)
        cols = {c["name"] for c in insp.get_columns(table)}
        entry = {
            "survey_id": survey_id,
            "columns": len(cols),
            "rows": int(conn.execute(text(f"SELECT COUNT(*) FROM {quoted}{clause}"), params).scalar_one()),
            "rows_are_estimated": False,
        }
        for dim in ("level", "report_year", "variable", S.DATASET_COL):
            if dim in cols:
                name = "datasets" if dim == S.DATASET_COL else f"distinct_{dim}"
                entry[name] = int(conn.execute(
                    text(f"SELECT COUNT(DISTINCT {quote(dim)}) FROM {quoted}{clause}"), params
                ).scalar_one())
        out[table] = entry
    return out


def export_layer(db: Session, layer: str, out_dir: Path | None = None,
                 dataset_ids: list[str] | None = None,
                 survey_id: int | None = None) -> dict:
    """Write one layer to CSV — a file per report year, utf-8-sig for Excel.

    `survey_id` exports only that survey's rows.
    """
    if layer not in S.LAYERS:
        raise ValueError(f"Unknown layer '{layer}'. Use one of: {', '.join(S.LAYERS)}")
    conn = db.connection()
    if not inspect(conn).has_table(layer):
        raise ValueError(f"{layer} does not exist yet.")

    out = Path(out_dir) if out_dir else EXPORTS_DIR / layer
    out.mkdir(parents=True, exist_ok=True)
    quoted = conn.dialect.identifier_preparer.quote(layer)
    # Scope to one job's datasets when asked, so an export need not pull a
    # multi-million-row layer to write the rows a single fetch produced.
    clause, params = survey.where(conn, layer, survey_id)
    sql = f"SELECT * FROM {quoted}{clause}"
    if dataset_ids:
        placeholders = ", ".join(f":d{i}" for i in range(len(dataset_ids)))
        sql += (" AND " if clause else " WHERE ") + \
            f"{conn.dialect.identifier_preparer.quote(S.DATASET_COL)} IN ({placeholders})"
        params |= {f"d{i}": v for i, v in enumerate(dataset_ids)}
    df = pd.read_sql_query(text(sql), conn, params=params)
    if df.empty:
        raise ValueError(
            f"{layer} is empty."
            if not dataset_ids else
            f"{layer} holds no rows for dataset(s) {', '.join(dataset_ids)} — "
            "the job may have written to a different layer, or not finished."
        )
    if layer == S.MASTER:
        # survey_id stays: master holds one row per place per survey.
        df = df.drop(columns=[S.DATASET_COL, S.INGESTED_COL], errors="ignore")

    written = []
    groups = df.groupby("report_year") if "report_year" in df.columns else [("all", df)]
    for year, part in groups:
        name = f"hmis_f1_{str(year).replace('/', '-')}.csv"
        path = out / name
        path.write_bytes(part.to_csv(index=False).encode("utf-8-sig"))
        written.append({"file": name, "rows": int(part.shape[0]),
                        "columns": int(part.shape[1]), "bytes": path.stat().st_size})
    return {"layer": layer, "directory": str(out), "files": len(written),
            "rows": sum(w["rows"] for w in written), "written": written}
