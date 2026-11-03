"""Scope a warehouse layer to one survey.

Every layer row carries `survey_id`, so any build, read or export can act on
one survey and leave the others in the shared table untouched.
"""
import pandas as pd
from sqlalchemy import inspect, text

SURVEY_COL = "survey_id"


def has_survey_col(conn, table: str) -> bool:
    return SURVEY_COL in {c["name"] for c in inspect(conn).get_columns(table)}


def where(conn, table: str, survey_id: int | None) -> tuple[str, dict]:
    """`WHERE survey_id = :survey_id` for `table`, or nothing when no survey is given.

    A layer built before surveys were carried through has no such column, and
    filtering it would silently return everything — so that is an error.
    """
    if survey_id is None:
        return "", {}
    if not has_survey_col(conn, table):
        raise ValueError(f"{table} has no {SURVEY_COL} column — rebuild it without a survey_id first.")
    quote = conn.dialect.identifier_preparer.quote
    return f" WHERE {quote(SURVEY_COL)} = :survey_id", {"survey_id": survey_id}


def clear(conn, table: str, survey_id: int) -> int:
    """Delete one survey's rows from a layer before that survey is rebuilt into it."""
    if not inspect(conn).has_table(table):
        return 0
    clause, params = where(conn, table, survey_id)
    quoted = conn.dialect.identifier_preparer.quote(table)
    return int(conn.execute(text(f"DELETE FROM {quoted}{clause}"), params).rowcount or 0)


def stamp(df: pd.DataFrame, survey_id) -> pd.DataFrame:
    """Set `survey_id` on every row, typed as a nullable integer.

    Int64 rather than object or float, so a chunk with no survey still creates
    a BIGINT column and `survey_id = 3` compares as a number.
    """
    df[SURVEY_COL] = pd.array([None if pd.isna(survey_id) else int(survey_id)] * len(df),
                              dtype="Int64")
    return df
