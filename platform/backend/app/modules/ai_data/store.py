"""Gemini's own tables: one row per value, so any source and any column fits.

Kept entirely apart from the census/HMIS warehouse and dashboard_master —
nothing here reads into or writes to those.

Why long, not wide: twenty prompts over different sources can name a thousand
different columns. As table columns that hits PostgreSQL's hard limit of 1,600
per table; as rows it is just more rows. Every table here has a fixed shape
however many sources or indicators arrive.

    gemini_fetches   one row per prompt: what was asked, what came back, the check
    gemini_raw       every value exactly as Gemini returned it — full history
    gemini_clean     indicator names standardised, numbers parsed, places resolved
                     to the district-master ids census and HMIS also use
    gemini_master    one value per (survey, source, place, year, indicator):
                     the newest fetch of that value wins

Overwriting: master's key includes the source and the indicator, so two prompts
under one survey never replace each other's data (population and hospital beds
are different indicators; Census 2011 and NFHS-5 are different sources).
Fetching the same source/indicator/place/year again replaces only that value —
a correction — and the earlier value stays in gemini_raw.
"""
import re
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.constants import district_crosswalk as crosswalk
from app.helpers.dataframe import snake_case
from app.models.common import new_id

FETCHES, RAW, CLEAN, MASTER = "gemini_fetches", "gemini_raw", "gemini_clean", "gemini_master"
LAYERS = (RAW, CLEAN, MASTER)

_DDL = [
    f"""CREATE TABLE IF NOT EXISTS {FETCHES} (
        id TEXT PRIMARY KEY, survey_id BIGINT NOT NULL, prompt TEXT NOT NULL, title TEXT,
        model TEXT, rows_returned INTEGER, values_stored INTEGER, columns JSONB,
        sources JSONB, provenance JSONB, created_at TIMESTAMPTZ NOT NULL)""",
    f"""CREATE TABLE IF NOT EXISTS {RAW} (
        fetch_id TEXT NOT NULL, survey_id BIGINT NOT NULL, fetched_at TIMESTAMPTZ NOT NULL,
        row_no INTEGER NOT NULL, place_level TEXT, state TEXT, district TEXT, year TEXT,
        column_name TEXT NOT NULL, value_raw TEXT, source_name TEXT, unit TEXT,
        estimated BOOLEAN NOT NULL DEFAULT FALSE)""",
    f"""CREATE TABLE IF NOT EXISTS {CLEAN} (
        fetch_id TEXT NOT NULL, survey_id BIGINT NOT NULL, fetched_at TIMESTAMPTZ NOT NULL,
        source_key TEXT NOT NULL, source_name TEXT, place_level TEXT NOT NULL, place_key TEXT NOT NULL,
        state_name TEXT, state_id DOUBLE PRECISION, district_name TEXT,
        district_id DOUBLE PRECISION, unique_id DOUBLE PRECISION, year TEXT NOT NULL,
        indicator TEXT NOT NULL, label TEXT, value DOUBLE PRECISION, value_text TEXT,
        unit TEXT, estimated BOOLEAN NOT NULL DEFAULT FALSE)""",
    f"CREATE TABLE IF NOT EXISTS {MASTER} (LIKE {CLEAN})",
    # A fetch runs in the background: its row exists from the request on and
    # says how far it got. Added in place so an existing table keeps its rows.
    f"ALTER TABLE {FETCHES} ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'succeeded'",
    f"ALTER TABLE {FETCHES} ADD COLUMN IF NOT EXISTS error TEXT",
    f"ALTER TABLE {FETCHES} ADD COLUMN IF NOT EXISTS use_search BOOLEAN",
    f"ALTER TABLE {FETCHES} ADD COLUMN IF NOT EXISTS result JSONB",
    f"ALTER TABLE {FETCHES} ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ",
    f"ALTER TABLE {FETCHES} ADD COLUMN IF NOT EXISTS progress TEXT",
    f"CREATE INDEX IF NOT EXISTS ix_{RAW}_survey ON {RAW} (survey_id, fetch_id)",
    f"CREATE INDEX IF NOT EXISTS ix_{CLEAN}_survey ON {CLEAN} (survey_id)",
    f"CREATE INDEX IF NOT EXISTS ix_{MASTER}_survey_ind ON {MASTER} (survey_id, indicator)",
    f"CREATE INDEX IF NOT EXISTS ix_{MASTER}_survey_src ON {MASTER} (survey_id, source_key)",
]

# Master key: what one stored value is *of*. Year is '' when the table has none.
MASTER_KEY = ("survey_id", "source_key", "place_level", "place_key", "year", "indicator")

# How Gemini may name place / time columns (matched on snake_case names).
_STATE = re.compile(r"(^|_)(state|states|union_territor\w*|ut|state_ut)(_|$)")
_DISTRICT = re.compile(r"(^|_)district(_name)?(_|$)")
_YEAR = re.compile(r"(^|_)(year|fy|financial_year|reference_year|census_year)(_|$)")
_NATIONAL = re.compile(r"^\s*(total|all\s*india|india)\b", re.I)
_BRACKETS = re.compile(r"\(.*?\)")
# Row counters carry no information about the place.
_SKIP = re.compile(r"^(s_?no|sr_?no|sl_?no|serial(_no)?|rank|row|index)$")


def ensure_tables(conn: Connection) -> None:
    for ddl in _DDL:
        conn.execute(text(ddl))


def source_key(name: str | None) -> str:
    """'Census of India 2011' -> 'census_of_india_2011'; unknown -> 'unspecified'."""
    return snake_case(name)[:80] if name and str(name).strip() else "unspecified"


# --------------------------------------------------------------------------
# Gemini's wide table -> raw rows (one per value)
# --------------------------------------------------------------------------

def _place_columns(columns) -> dict[str, str | None]:
    found = {"state": None, "district": None, "year": None}
    for col in columns:
        key = snake_case(col)
        for role, pattern in (("district", _DISTRICT), ("state", _STATE), ("year", _YEAR)):
            if found[role] is None and pattern.search(key):
                found[role] = col
                break
    return found


def to_raw(df: pd.DataFrame, table: dict, fetch_id: str, survey_id: int,
           fetched_at: datetime) -> pd.DataFrame:
    """Melt Gemini's table into one row per (row, value column)."""
    place = _place_columns(df.columns)
    place_cols = {c for c in place.values() if c}
    value_cols = [c for c in df.columns if c not in place_cols and not _SKIP.match(snake_case(c))]

    meta = table.get("column_sources") or {}
    estimated = {str(c) for c in table.get("estimated_columns") or []}
    default_source = next((s.get("name") for s in table.get("sources") or [] if s.get("name")), None)

    out = []
    for row_no, row in enumerate(df.itertuples(index=False), start=1):
        record = dict(zip(df.columns, row))
        state = record.get(place["state"]) if place["state"] else None
        district = record.get(place["district"]) if place["district"] else None
        if state is not None and _NATIONAL.match(str(state)):
            level, state = "national", None
        else:
            level = "district" if district else ("state" if state else "national")
        year = record.get(place["year"]) if place["year"] else None
        for col in value_cols:
            value = record.get(col)
            # Kept even when empty: Gemini leaves a value null when it will not
            # guess it (e.g. a district figure only published at state level),
            # and the column must still show that it was asked for.
            if isinstance(value, float) and pd.isna(value):
                value = None
            info = meta.get(col) or {}
            source = info.get("source") or default_source
            out.append({
                "fetch_id": fetch_id, "survey_id": survey_id, "fetched_at": fetched_at,
                "row_no": row_no, "place_level": level,
                "state": None if state is None else str(state),
                "district": None if district is None else str(district),
                "year": None if year is None else re.sub(r"\.0$", "", str(year)),
                "column_name": str(col), "value_raw": None if value is None else str(value),
                "source_name": source, "unit": info.get("unit"),
                "estimated": col in estimated or "estimate" in str(source or "").lower(),
            })
    return pd.DataFrame(out)


# --------------------------------------------------------------------------
# raw -> clean -> master, scoped to one survey
# --------------------------------------------------------------------------

def _number(value):
    if value is None:
        return None
    cleaned = re.sub(r"[,%+\s₹]", "", str(value))
    try:
        return float(cleaned)
    except ValueError:
        return None


def _resolve(raw: pd.DataFrame) -> pd.DataFrame:
    """Place names -> district-master ids, once per distinct place."""
    places = raw[["place_level", "state", "district"]].drop_duplicates()
    rows = []
    for level, state, district in places.itertuples(index=False):
        lookup = _BRACKETS.sub("", state or "").strip()
        st = crosswalk.lookup_state(lookup) if lookup else None
        dist = crosswalk.lookup_district(lookup, district) if district else None
        if level == "district":
            key = f"d:{int(dist['unique_id'])}" if dist else \
                f"dn:{crosswalk.normalise(lookup)}|{crosswalk.normalise(district)}"
        elif level == "state":
            key = f"s:{int(st['state_id'])}" if st else f"sn:{crosswalk.normalise(lookup)}"
        else:
            key = "india"
        rows.append({
            "place_level": level, "state": state, "district": district, "place_key": key,
            "state_name": (st or {}).get("state") or (lookup or None),
            "state_id": (st or {}).get("state_id"),
            "district_name": (dist or {}).get("district") or district,
            "district_id": (dist or {}).get("district_id"),
            "unique_id": (dist or {}).get("unique_id"),
        })
    return raw.merge(pd.DataFrame(rows), on=["place_level", "state", "district"], how="left")


def build(conn: Connection, survey_id: int) -> dict:
    """Rebuild this survey's clean and master rows from its raw rows."""
    raw = pd.read_sql_query(text(f"SELECT * FROM {RAW} WHERE survey_id = :s"), conn,
                            params={"s": survey_id})
    conn.execute(text(f"DELETE FROM {CLEAN} WHERE survey_id = :s"), {"s": survey_id})
    conn.execute(text(f"DELETE FROM {MASTER} WHERE survey_id = :s"), {"s": survey_id})
    if raw.empty:
        return {"survey_id": survey_id, "raw": 0, "clean": 0, "master": 0, "unresolved_places": []}

    clean = _resolve(raw)
    clean["source_key"] = clean["source_name"].map(source_key)
    clean["indicator"] = clean["column_name"].map(lambda c: snake_case(c)[:120])
    clean["label"] = clean["column_name"]
    clean["value"] = clean["value_raw"].map(_number)
    clean["value_text"] = clean["value_raw"].where(clean["value"].isna())
    clean["year"] = clean["year"].fillna("")
    columns = ["fetch_id", "survey_id", "fetched_at", "source_key", "source_name", "place_level",
               "place_key", "state_name", "state_id", "district_name", "district_id", "unique_id",
               "year", "indicator", "label", "value", "value_text", "unit", "estimated"]
    clean = clean[columns]
    for col in ("state_id", "district_id", "unique_id"):
        clean[col] = pd.to_numeric(clean[col], errors="coerce").astype("float64")

    # Newest fetch wins per key — but an empty value never replaces a real one:
    # rows with a value sort after empty ones, so `keep="last"` takes the
    # newest real value and falls back to empty only when nothing else exists.
    # A stable sort, so the same input always keeps the same row.
    has_value = clean["value"].notna() | clean["value_text"].notna()
    master = (clean.assign(_order=range(len(clean)), _has=has_value)
              .sort_values(["_has", "fetched_at", "fetch_id", "_order"], kind="mergesort")
              .drop_duplicates(subset=list(MASTER_KEY), keep="last")
              .drop(columns=["_order", "_has"]))

    clean.to_sql(CLEAN, conn, if_exists="append", index=False)
    master.to_sql(MASTER, conn, if_exists="append", index=False)
    # Places the district master did not know (kept, under their own names).
    unknown = clean[clean["place_key"].str.startswith(("dn:", "sn:"))]
    unresolved = sorted({f"{s}{' / ' + d if d else ''}"
                         for s, d in zip(unknown["state_name"].fillna(""),
                                         unknown["district_name"].fillna(""))})
    return {"survey_id": survey_id, "raw": int(len(raw)), "clean": int(len(clean)),
            "master": int(len(master)), "replaced_by_newer_fetch": int(len(clean) - len(master)),
            "unresolved_places": unresolved}


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

FILTERS = ("source_key", "place_level", "state_name", "district_name", "year", "indicator",
           "fetch_id")


def where(survey_id: int, filters: dict) -> tuple[str, dict]:
    clauses, params = ["survey_id = :survey_id"], {"survey_id": survey_id}
    for key in FILTERS:
        value = filters.get(key)
        if value is None:
            continue
        values = value if isinstance(value, (list, tuple)) else [value]
        names = [f"{key}_{i}" for i in range(len(values))]
        clauses.append(f"{key} IN ({', '.join(':' + n for n in names)})")
        params |= dict(zip(names, values))
    return " WHERE " + " AND ".join(clauses), params


def read(conn: Connection, layer: str, survey_id: int, filters: dict,
         limit: int | None = None, offset: int = 0) -> tuple[int, pd.DataFrame]:
    clause, params = where(survey_id, filters)
    total = conn.execute(text(f"SELECT COUNT(*) FROM {layer}{clause}"), params).scalar_one()
    order = ("fetched_at, fetch_id, row_no, column_name" if layer == RAW
             else "source_key, indicator, place_key, year")
    sql = f"SELECT * FROM {layer}{clause} ORDER BY {order}"
    if limit is not None:
        sql += " LIMIT :_limit OFFSET :_offset"
        params |= {"_limit": limit, "_offset": offset}
    return int(total), pd.read_sql_query(text(sql), conn, params=params)


def indicators(conn: Connection, survey_id: int, source: str | None = None) -> pd.DataFrame:
    clause, params = where(survey_id, {"source_key": source})
    return pd.read_sql_query(text(f"""
        SELECT indicator, MIN(label) AS label, source_key, MIN(source_name) AS source_name,
               MIN(unit) AS unit, BOOL_OR(estimated) AS estimated,
               COUNT(*) AS values, COUNT(DISTINCT place_key) AS places,
               STRING_AGG(DISTINCT place_level, ',') AS levels,
               STRING_AGG(DISTINCT NULLIF(year, ''), ',') AS years,
               MAX(fetched_at) AS last_fetched
        FROM {MASTER}{clause}
        GROUP BY indicator, source_key ORDER BY source_key, indicator"""), conn, params=params)


def wide(master: pd.DataFrame, max_columns: int) -> tuple[pd.DataFrame, bool]:
    """One row per place (and year), one column per source+indicator — for viewing/export."""
    if master.empty:
        return master, False
    master = master.assign(
        _col=master["indicator"] + "__" + master["source_key"],
        _val=master["value"].where(master["value"].notna(), master["value_text"]))
    columns = list(dict.fromkeys(master["_col"]))
    truncated = len(columns) > max_columns
    master = master[master["_col"].isin(columns[:max_columns])]
    index = ["place_level", "state_name", "district_name", "unique_id", "year"]
    # A pivot drops rows whose index holds a null — every state-level row has
    # no district or unique_id — so empty index values are filled first.
    master[index] = master[index].astype(object).where(master[index].notna(), "")
    out = (master.pivot_table(index=index, columns="_col", values="_val", aggfunc="last")
           .reset_index())
    out.columns.name = None
    return out.dropna(axis=1, how="all"), truncated


# --------------------------------------------------------------------------
# Warehouse-shaped views: one row per place, one column per indicator
# --------------------------------------------------------------------------
# Stored one row per value (fixed width, no overwriting); served in the same
# shape as /warehouse/{census|hmis}/rows — meta columns, then indicators.

WAREHOUSE_LAYERS = {RAW: "gemini_raw", CLEAN: "gemini_clean", MASTER: "gemini_master"}

# Per layer: (index columns of the stored rows, their names in the response).
_INDEX = {
    RAW: (["fetch_id", "fetched_at", "row_no", "place_level", "state", "district", "year"],
          {"fetch_id": "_dataset_id", "fetched_at": "_ingested_at", "place_level": "level",
           "state": "state_name", "district": "district_name"}),
    CLEAN: (["fetch_id", "fetched_at", "place_level", "place_key", "state_name", "state_id",
             "district_name", "district_id", "unique_id", "year"],
            {"fetch_id": "_dataset_id", "fetched_at": "_ingested_at", "place_level": "level"}),
    MASTER: (["place_level", "place_key", "state_name", "state_id", "district_name",
              "district_id", "unique_id", "year"],
             {"place_level": "level"}),
}
META = ("_dataset_id", "_ingested_at", "survey_id", "level", "state_name", "state_id",
        "district_name", "district_id", "unique_id", "year")
_SORT = ["level", "state_name", "district_name", "year"]


def _column_names(stored: pd.DataFrame, layer: str) -> pd.Series:
    """Indicator column per stored value. raw keeps Gemini's own column name;
    clean/master use the standard indicator name, adding the source only when
    two sources give the same indicator, so columns never collide."""
    if layer == RAW:
        return stored["column_name"]
    sources = stored.groupby("indicator")["source_key"].nunique()
    shared = set(sources[sources > 1].index)
    return stored["indicator"].where(~stored["indicator"].isin(shared),
                                     stored["indicator"] + "__" + stored["source_key"])


def _cell(stored: pd.DataFrame, layer: str) -> pd.Series:
    """The value to show: the number when there is one, else the text."""
    if layer == RAW:
        numeric = stored["value_raw"].map(_number)
        return numeric.where(numeric.notna(), stored["value_raw"])
    return stored["value"].where(stored["value"].notna(), stored["value_text"])


def warehouse_frame(conn: Connection, layer: str, survey_id: int,
                    fetch_id: str | None = None,
                    full_columns: list[str] | None = None) -> pd.DataFrame:
    """A whole layer for one survey, wide: META columns, then indicator columns.

    `fetch_id` (master only): just the places and years that fetch covered —
    with their final values, whichever fetch supplied each.
    `full_columns`: the complete indicator list to show (a source's warehouse
    columns, in order); any of them with no value yet appears empty.
    """
    df = _warehouse_frame(conn, layer, survey_id, fetch_id)
    if full_columns:
        meta = [c for c in META if c in df.columns]
        extra = [c for c in df.columns if c not in meta and c not in set(full_columns)]
        df = df.reindex(columns=meta + list(full_columns) + extra)
    return df


def _warehouse_frame(conn: Connection, layer: str, survey_id: int,
                     fetch_id: str | None = None) -> pd.DataFrame:
    stored = pd.read_sql_query(text(f"SELECT * FROM {layer} WHERE survey_id = :s"),
                               conn, params={"s": survey_id})
    if fetch_id is not None and not stored.empty:
        covered = pd.read_sql_query(
            text(f"SELECT DISTINCT place_key, year FROM {CLEAN} WHERE fetch_id = :f"),
            conn, params={"f": fetch_id})
        stored = stored.merge(covered, on=["place_key", "year"], how="inner")
    if stored.empty:
        return pd.DataFrame(columns=list(META))
    index, rename = _INDEX[layer]
    stored = stored.assign(_col=_column_names(stored, layer), _val=_cell(stored, layer))
    if layer == MASTER:
        # Master merges many fetches into one row per place: its stamp is the
        # newest fetch that contributed.
        latest = stored.groupby("place_key")["fetched_at"].max()
    # Grouping drops rows whose index holds a null (state rows have no district).
    stored[index] = stored[index].astype(object).where(stored[index].notna(), "")
    # group + unstack, not pivot_table: pivot_table silently drops a place whose
    # values are all empty and a column that is empty everywhere. Gemini leaves
    # values empty when it will not guess them — those places and columns must
    # still appear (a 38-district answer otherwise showed only 16 rows).
    wide = (stored.groupby(index + ["_col"], sort=False)["_val"].last()
            .unstack("_col").reset_index())
    wide.columns.name = None
    wide = wide.replace({"": None}).rename(columns=rename)
    if layer == MASTER:
        wide["_ingested_at"] = wide["place_key"].map(latest)
    wide["survey_id"] = survey_id
    wide = wide.drop(columns=["place_key", "row_no"], errors="ignore")
    indicators_ = sorted(c for c in wide.columns if c not in META)
    meta = [c for c in META if c in wide.columns]
    return wide[meta + indicators_].sort_values(
        [c for c in _SORT if c in wide.columns], na_position="first", kind="mergesort"
    ).reset_index(drop=True)


def now() -> datetime:
    return datetime.now(timezone.utc)


def new_fetch_id() -> str:
    return new_id()
