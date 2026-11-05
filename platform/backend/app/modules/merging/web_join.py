"""Merge with Website tables: join on district and year, not district alone.

Census describes a district once, so it joins on `unique_id`. HMIS and every
Website table describe a district once per year, so they join each other on
(`unique_id`, `year`): a Website value for 2019-2020 lands on that district's
2019-2020 HMIS rows, never on 2018-2019. The result has one shared `year`
column (HMIS's report_year, or the Website year where HMIS has no row).

Each Website table becomes columns named from its table name, so the same
table saved for several years fills the same columns on different rows.
Spec: docs/superpowers/specs/2026-11-05-website-warehouse-design.md §5.4.
"""
import functools
import hashlib
import re

import pandas as pd
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.helpers.dataframe import snake_case
from app.models import WebMaster, WebTable
from app.models.web_base import parse_key
from app.modules.merging.schema import DASHBOARD_TABLE
from app.modules.web_warehouse import schema as web_schema

KEY = "unique_id"
YEAR = web_schema.YEAR_COL
MAX_NAME = 63  # Postgres identifier limit


class WebMergeError(ValueError):
    """A Website table can't be merged as asked; the merge job reports the message."""


def _slug(name: str) -> str:
    """snake_case of the table name; a name with no Latin letters gets a stable hash."""
    if not re.search(r"[A-Za-z0-9]", name):
        return "t" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    return snake_case(name)


def web_column(table_name: str, indicator: str) -> str:
    """`web_<table>_<indicator>`, cut to 63 characters with a hash keeping it unique."""
    full = f"{web_schema.COLUMN_PREFIX}{_slug(table_name)}_{indicator}"
    if len(full) <= MAX_NAME:
        return full
    return f"{full[:56]}_{hashlib.sha1(full.encode('utf-8')).hexdigest()[:6]}"


def _as_key(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").astype("Int64")


def load_web_frames(db: Session, survey_id: int,
                    web_table_ids: list[str]) -> tuple[list[pd.DataFrame], pd.DataFrame]:
    """(one wide frame per table name keyed unique_id + year, the places they name)."""
    groups: dict[str, list[tuple[WebTable, list[WebMaster]]]] = {}
    for table_id in web_table_ids:
        key = parse_key(table_id)
        table = db.query(WebTable).filter(WebTable.external_id == key).first() if key else None
        if table is None or table.survey_id != survey_id:
            raise WebMergeError(f"Website table '{table_id}' is not part of survey {survey_id}.")
        rows = db.query(WebMaster).filter(WebMaster.web_table_id == table.id).all()
        if not rows:
            raise WebMergeError(
                f"Website table '{table.name}' has no master rows; rebuild its master first.")
        groups.setdefault(_slug(table.name), []).append((table, rows))

    frames, places = [], []
    owner: dict[str, str] = {}  # dashboard column -> the table name filling it
    for members in groups.values():
        records = [{"table_id": t.id, "name": t.name, KEY: r.unique_id, YEAR: r.year,
                    "indicator": r.indicator, "value": r.value,
                    "state_name": r.state_name, "district_name": r.district_name,
                    "state_id": r.state_id, "district_id": r.district_id}
                   for t, rows in members for r in rows]
        long = pd.DataFrame(records)
        clash = long[long.duplicated([KEY, YEAR, "indicator"], keep=False)]
        if not clash.empty:
            first = clash.iloc[0]
            raise WebMergeError(
                f"Two ticked Website tables are both called '{first['name']}' and both have "
                f"'{first['indicator']}' for {first[YEAR]}; rename one.")
        name = members[0][0].name
        wide = long.pivot(index=[KEY, YEAR], columns="indicator", values="value").reset_index()
        wide.columns.name = None
        wide = wide.rename(columns={c: web_column(name, c) for c in wide.columns if c not in (KEY, YEAR)})
        for column in wide.columns:
            if column in (KEY, YEAR):
                continue
            if column in owner:
                raise WebMergeError(f"Website tables '{owner[column]}' and '{name}' would both fill "
                                    f"column '{column}'; rename one.")
            owner[column] = name
        wide[KEY] = _as_key(wide[KEY])
        frames.append(wide)
        places.append(long[[KEY, "state_name", "district_name", "state_id", "district_id"]])

    place_frame = (pd.concat(places).drop_duplicates(KEY)
                   .rename(columns={"state_name": "web_state_name", "district_name": "web_district_name",
                                    "state_id": "web_state_id", "district_id": "web_district_id"}))
    place_frame[KEY] = _as_key(place_frame[KEY])
    return frames, place_frame


def _keyed(df: pd.DataFrame, name: str, report: list[str]) -> pd.DataFrame:
    """Rows with a district id. Rows without one are a source's own totals."""
    df = df.copy()
    df[KEY] = _as_key(df[KEY])
    blank = df[KEY].isna()
    if blank.any():
        report.append(f"Dropped rows with no '{KEY}' ({name}: {int(blank.sum()):,}) — unjoinable, "
                      "and typically a source's own subtotal of rows already present.")
    return df[~blank]


def _suffixed(df: pd.DataFrame, name: str) -> pd.DataFrame:
    return df.rename(columns={c: f"{c}__{name}" for c in df.columns if c != KEY})


def join_with_web(census: pd.DataFrame | None, hmis: pd.DataFrame | None,
                  web: list[pd.DataFrame],
                  places: pd.DataFrame | None = None) -> tuple[pd.DataFrame, list[str]]:
    """Year-varying sources joined on (unique_id, year), then Census on unique_id."""
    report: list[str] = []
    timed: list[pd.DataFrame] = []
    if hmis is not None:
        h = _keyed(hmis, "hmis", report)
        year = h["report_year"].astype(object)
        h = _suffixed(h, "hmis")
        h[YEAR] = year
        timed.append(h)
    timed += [_keyed(frame, "website", report) for frame in web]

    merged = functools.reduce(
        lambda left, right: pd.merge(left, right, on=[KEY, YEAR], how="outer"), timed)
    if census is not None:
        merged = pd.merge(merged, _suffixed(_keyed(census, "census", report), "census"),
                          on=KEY, how="outer")
    if places is not None and not places.empty:
        merged = merged.merge(places, on=KEY, how="left")
    merged = merged[[KEY, YEAR] + [c for c in merged.columns if c not in (KEY, YEAR)]]
    sources = (["census"] if census is not None else []) + (["hmis"] if hmis is not None else [])
    report.append(f"Joined {', '.join(sources + [f'{len(web)} Website table name(s)'])} on "
                  f"'{KEY}' (+ '{YEAR}' for HMIS and Website) -> {merged.shape[0]:,} rows, "
                  f"{merged.shape[1]:,} columns.")
    return merged.reset_index(drop=True), report


def check_column_budget(conn, df: pd.DataFrame) -> None:
    """Refuse a write that would take dashboard_master past the column limit.

    Postgres counts dropped columns towards its 1,600, so they are counted too.
    """
    insp = inspect(conn)
    if insp.has_table(DASHBOARD_TABLE):
        names = {c["name"] for c in insp.get_columns(DASHBOARD_TABLE)}
        if conn.dialect.name == "postgresql":
            existing = int(conn.execute(text(
                "SELECT count(*) FROM pg_attribute "
                "WHERE attrelid = to_regclass(:t) AND attnum > 0"), {"t": DASHBOARD_TABLE}).scalar_one())
        else:
            existing = len(names)
    else:
        names, existing = set(), 0
    new = len([c for c in df.columns if c not in names])
    total = existing + new
    limit = web_schema.MAX_DASHBOARD_COLUMNS
    # A merge that adds no column never makes things worse; refusing it would
    # stop every survey re-merging once the shared table is past the limit.
    if new and total > limit:
        raise WebMergeError(
            f"This merge would give {DASHBOARD_TABLE} {total:,} columns; the limit is {limit:,}. "
            "Untick some Website tables or reuse table names.")
