"""Merge one survey's sources into public.dashboard_master.

A survey is the link between warehouses: census and HMIS fetched under the
same survey_id describe the same places, so merging them is how a dashboard
gets a denominator beside a measure.

Always the master layer. raw and clean hold one row per pull — several pulls
of the same district — so joining them would multiply rows; master is already
one row per place.
"""
import pandas as pd
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.modules.census_warehouse import schema as census_schema
from app.modules.hmis_warehouse import schema as hmis_schema
from app.modules.merging.schema import (  # noqa: F401  (re-exported)
    CLEAN_CONNECTOR,
    DASHBOARD_TABLE,
    DATASET_COL,
    INGESTED_COL,
    LAYERS,
    MERGE_CONNECTOR,
    META_COLUMNS,
    SURVEY_COL,
)

# Every warehouse a survey can draw on. A new connector is added here and
# needs no change in the merge itself.
WAREHOUSES: dict[str, object] = {
    "census": census_schema,
    "hmis": hmis_schema,
}


class SurveyMergeError(Exception):
    pass


def available_connectors(db: Session, survey_id: int) -> list[str]:
    """Which warehouses actually hold rows for this survey."""
    conn = db.connection()
    insp = inspect(conn)
    quote = conn.dialect.identifier_preparer.quote

    found = []
    for name, schema in WAREHOUSES.items():
        table = schema.MASTER
        if not insp.has_table(table):
            continue
        if schema.SURVEY_COL not in {c["name"] for c in insp.get_columns(table)}:
            continue
        rows = conn.execute(
            text(f"SELECT 1 FROM {quote(table)} WHERE {quote(schema.SURVEY_COL)} = :s LIMIT 1"),
            {"s": survey_id},
        ).first()
        if rows:
            found.append(name)
    return found


def load_master(db: Session, connector: str, survey_id: int) -> pd.DataFrame:
    """One warehouse's master rows for this survey."""
    schema = WAREHOUSES[connector]
    conn = db.connection()
    quote = conn.dialect.identifier_preparer.quote
    df = pd.read_sql_query(
        text(f"SELECT * FROM {quote(schema.MASTER)} WHERE {quote(schema.SURVEY_COL)} = :s"),
        conn, params={"s": survey_id},
    )
    if df.empty:
        raise SurveyMergeError(
            f"{schema.MASTER} holds no rows for survey {survey_id}. "
            f"Fetch it first, then build that warehouse's master layer."
        )
    return df


def load_dashboard(db: Session, survey_id: int | None = None) -> pd.DataFrame:
    """dashboard_master rows, optionally just one survey's."""
    conn = db.connection()
    if not inspect(conn).has_table(DASHBOARD_TABLE):
        raise SurveyMergeError(
            f"{DASHBOARD_TABLE} does not exist yet — run POST /datasets/merge first."
        )
    quote = conn.dialect.identifier_preparer.quote
    sql = f"SELECT * FROM {quote(DASHBOARD_TABLE)}"
    params: dict = {}
    if survey_id is not None:
        sql += f" WHERE {quote(SURVEY_COL)} = :s"
        params["s"] = survey_id
    df = pd.read_sql_query(text(sql), conn, params=params)
    if df.empty:
        scope = f" for survey {survey_id}" if survey_id is not None else ""
        raise SurveyMergeError(f"{DASHBOARD_TABLE} holds no rows{scope}.")
    return df


def replace_survey(db: Session, survey_id: int) -> int:
    """Clear this survey's rows from dashboard_master before rewriting them.

    Scoped to the survey so re-merging one never disturbs another's rows.
    """
    return clear_dashboard(db, survey_id)


def clear_dashboard(db: Session, survey_id: int | None = None) -> int:
    """Delete dashboard_master rows — one survey's, or all of them.

    `survey_id=None` really does clear the table, so callers pass it only when
    the caller asked for every survey; `replace_survey` is the scoped door in.
    """
    conn = db.connection()
    if not inspect(conn).has_table(DASHBOARD_TABLE):
        return 0
    quote = conn.dialect.identifier_preparer.quote
    sql = f"DELETE FROM {quote(DASHBOARD_TABLE)}"
    params: dict = {}
    if survey_id is not None:
        cols = {c["name"] for c in inspect(conn).get_columns(DASHBOARD_TABLE)}
        if SURVEY_COL not in cols:
            return 0
        sql += f" WHERE {quote(SURVEY_COL)} = :s"
        params["s"] = survey_id
    removed = conn.execute(text(sql), params).rowcount
    db.commit()
    return int(removed or 0)
