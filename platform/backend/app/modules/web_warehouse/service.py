"""The Website warehouse: web_raw -> web_clean -> web_master, one saved table at a time.

Exposes the same functions as census_warehouse.service / hmis_warehouse.service
(summary, build_clean, build_master, export_layer), so the generic
/warehouse/{connector} router serves it as connector `web`.

Each Website table has its own columns, so values are stored one per row.
Clean rebuilds a table in its original shape, runs the same cleaning
operations the Clean pop-up offers, then matches every place to the district
master (scoped to the state) and turns each cell back into one row.
Spec: docs/superpowers/specs/2026-11-05-website-warehouse-design.md §5.2-5.3.
"""
from collections import Counter
from pathlib import Path

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.constants import district_crosswalk
from app.core.config import EXPORTS_DIR
from app.helpers.dataframe import snake_case
from app.models import WebClean, WebMaster, WebRaw, WebTable
from app.models.web_base import bulk_insert, parse_key
from app.modules.cleaning import service as cleaning
from app.modules.scraping.scoring import parse_number
from app.modules.web_warehouse import schema as S
from app.modules.web_warehouse.years import normalise_year

LIST_LIMIT = 20
# Applied here rather than by clean_dataframe: the rebuilt table carries
# `row_number`, which makes every row unique and never empty.
ROW_OPERATIONS = ("drop_duplicates", "drop_empty_rows")
# Reserved names in the rebuilt table. Value columns become v0..vN, so no
# header can collide with these, standardize_columns leaves them alone, and
# standardize_states sees `state`.
ROW, DISTRICT, STATE, YEAR = "row_number", "district", "state", "year"


def select_tables(db: Session, survey_id: int | None, table: str | None) -> list[WebTable]:
    query = db.query(WebTable)
    if survey_id is not None:
        query = query.filter(WebTable.survey_id == survey_id)
    if table is not None:
        query = query.filter(WebTable.external_id == parse_key(table)) if parse_key(table) \
            else query.filter(False)
    found = query.order_by(WebTable.id).all()
    if not found:
        if table is not None:
            raise ValueError(f"Website table '{table}' not found.")
        raise ValueError("No Website tables found." if survey_id is None
                         else f"No Website tables found for survey {survey_id}.")
    return found


# A Website table's own total rows. Not districts: matching them would fail
# (or worse, hit a district of the same name), and merging them would count
# their districts twice.
TOTAL_ROWS = {"total", "grand total", "state total", "all india", "india", "overall", "sub total",
              "subtotal"}


def is_total_row(name: str) -> bool:
    text = " ".join(str(name).lower().replace(":", " ").split())
    return district_crosswalk.is_summary_row(name) or text in TOTAL_ROWS \
        or text.startswith(("total ", "grand total"))


def _blank(value) -> bool:
    return value is None or (isinstance(value, float) and pd.isna(value)) or str(value).strip() == ""


def _indicators(headers: list[str]) -> dict[str, str]:
    """Header -> snake_case indicator, unique within the table (total, total_2).

    The suffix keeps counting until the name is free, so "Total %", "Total"
    and "Total 2" can't end up sharing total_2.
    """
    taken: set[str] = set()
    out = {}
    for header in headers:
        base = name = snake_case(header)
        n = 1
        while name in taken:
            n += 1
            name = f"{base}_{n}"
        taken.add(name)
        out[header] = name
    return out


def _rebuild(db: Session, table: WebTable) -> tuple[pd.DataFrame, dict[str, str], dict, list[str]]:
    """The table in its original shape: (frame, {vK: header}, {(row, header): raw id},
    places with a repeated indicator).

    A long table (one source row per place per indicator) is folded to one row
    per place and year, its indicators side by side, numbered by the place's
    first source row. Otherwise each place would look repeated once per
    indicator. Two values for one place, year and indicator (Total and Rural
    rows, say) can't both be kept: the last wins and the place is reported.
    """
    raws = (db.query(WebRaw).filter(WebRaw.web_table_id == table.id)
            .order_by(WebRaw.row_number, WebRaw.id).all())
    headers = list(dict.fromkeys(r.column_name for r in raws))
    slot = {h: f"v{i}" for i, h in enumerate(headers)}
    rows: dict = {}
    raw_ids = {}
    repeated: list[str] = []
    for r in raws:
        key = (r.place_text, r.state_text, r.year_text) if table.indicator_column else r.row_number
        row = rows.setdefault(key, {ROW: r.row_number})
        if table.place_column:
            row[DISTRICT] = r.place_text
        if table.state_column:
            row[STATE] = r.state_text
        if table.year_column:
            row[YEAR] = r.year_text
        if table.indicator_column and slot[r.column_name] in row and r.place_text not in repeated:
            repeated.append(r.place_text)
        row[slot[r.column_name]] = r.value_text
        raw_ids[(row[ROW], r.column_name)] = r.id
    columns = [ROW] + [c for c, on in ((DISTRICT, table.place_column), (STATE, table.state_column),
                                       (YEAR, table.year_column)) if on] + list(slot.values())
    frame = pd.DataFrame(list(rows.values()), columns=columns)
    return frame, {v: h for h, v in slot.items()}, raw_ids, repeated


def _clean_frame(frame: pd.DataFrame, operations: list[str]) -> pd.DataFrame:
    rest = [op for op in operations if op not in ROW_OPERATIONS]
    if rest:  # an empty list would make clean_dataframe apply its defaults
        frame, _notes = cleaning.clean_dataframe(frame, rest)
    content = [c for c in frame.columns if c != ROW]
    if "drop_empty_rows" in operations and content:
        frame = frame.dropna(how="all", subset=content)
    if "drop_duplicates" in operations and content:
        frame = frame.drop_duplicates(subset=content)
    return frame


def _check_operations(operations: list[str]) -> None:
    unknown = [op for op in operations if op not in cleaning.OPERATIONS]
    if unknown:
        raise ValueError(f"Unknown cleaning operation(s): {', '.join(unknown)}. "
                         f"Available: {', '.join(cleaning.OPERATIONS)}")


def _clean_table(db: Session, table: WebTable, operations: list[str]) -> int:
    frame, slots, raw_ids, repeated = _rebuild(db, table)
    indicators = _indicators(list(slots.values()))
    frame = _clean_frame(frame, operations)

    out: list[dict] = []
    places: dict[str, bool] = {}  # distinct place name -> matched
    unrecognised: list[str] = []
    seen_rows: Counter = Counter()
    for record in frame.to_dict(orient="records"):
        district = None if _blank(record.get(DISTRICT)) else str(record[DISTRICT]).strip()
        if district and is_total_row(district):
            continue
        state = None if _blank(record.get(STATE)) else str(record[STATE]).strip()
        state = state or table.state_name
        if table.year:
            year = table.year
        else:
            year = normalise_year(record.get(YEAR))
            text = record.get(YEAR)
            if year is None and not _blank(text) and str(text) not in unrecognised:
                unrecognised.append(str(text))
        hit = district_crosswalk.lookup_district(state, district) if (state and district) else None
        if district:
            places[district] = places.get(district, False) or hit is not None
        if hit:
            seen_rows[(hit["unique_id"], year, hit["district"])] += 1
        row_number = int(record[ROW])
        geo = {
            "state_name": hit["state"] if hit else state,
            "state_id": hit["state_id"] if hit else None,
            "district_name": hit["district"] if hit else district,
            "district_id": hit["district_id"] if hit else None,
            "unique_id": hit["unique_id"] if hit else None,
            "matched": hit is not None,
        }
        for slot, header in slots.items():
            if slot not in frame.columns:  # dropped by drop_empty_columns
                continue
            raw_id = raw_ids.get((row_number, header))
            if raw_id is None:  # a long table's place without this indicator
                continue
            out.append({"survey_id": table.survey_id, "web_table_id": table.id,
                        "raw_id": raw_id, **geo, "year": year,
                        "indicator": indicators[header], "value": parse_number(record.get(slot))})

    duplicates = list(dict.fromkeys(
        [name for (_u, _y, name), n in seen_rows.items() if n > 1] + [p for p in repeated if p]))
    db.query(WebClean).filter(WebClean.web_table_id == table.id).delete(synchronize_session=False)
    bulk_insert(db.connection(), WebClean, out)
    table.status = "cleaned"
    table.match_summary = {
        "matched": sum(places.values()),
        "total": len(places),
        "unmatched": [name for name, ok in places.items() if not ok][:LIST_LIMIT],
        "year_unrecognised": unrecognised[:LIST_LIMIT],
        "duplicate_places": duplicates[:LIST_LIMIT],
    }
    db.commit()
    return len(out)


def build_clean(db: Session, operations: list[str] | None = None, survey_id: int | None = None,
                table: str | None = None) -> dict:
    """web_raw -> web_clean for the survey's Website tables, or just `table`."""
    ops = list(operations or cleaning.WAREHOUSE_OPERATIONS)
    _check_operations(ops)
    results = []
    for web_table in select_tables(db, survey_id, table):
        rows = _clean_table(db, web_table, ops)
        results.append({"web_table_id": web_table.key, "name": web_table.name, "rows": rows,
                        "match_summary": web_table.match_summary})
    return {"table": S.CLEAN, "survey_id": survey_id, "rows": sum(r["rows"] for r in results),
            "tables": results}



# Master ----------------------------------------------------------------------
def _master_rows(db: Session, table: WebTable) -> list[dict]:
    """Matched rows with a year and a value; per district + year + indicator the
    one from the latest source row wins."""
    rows = (db.query(WebClean, WebRaw.row_number)
            .outerjoin(WebRaw, WebRaw.id == WebClean.raw_id)
            .filter(WebClean.web_table_id == table.id, WebClean.matched.is_(True),
                    WebClean.year.is_not(None), WebClean.value.is_not(None))
            .order_by(WebRaw.row_number, WebClean.id).all())
    latest: dict[tuple, dict] = {}
    for clean, _row in rows:
        latest[(clean.unique_id, clean.year, clean.indicator)] = {
            "survey_id": table.survey_id, "web_table_id": table.id,
            "state_name": clean.state_name, "state_id": clean.state_id,
            "district_name": clean.district_name, "district_id": clean.district_id,
            "unique_id": clean.unique_id, "year": clean.year,
            "indicator": clean.indicator, "value": clean.value}
    return list(latest.values())


def build_master(db: Session, survey_id: int | None = None, table: str | None = None) -> dict:
    """web_clean -> web_master for the cleaned tables; tables not cleaned are skipped."""
    results, skipped = [], []
    for web_table in select_tables(db, survey_id, table):
        db.query(WebMaster).filter(WebMaster.web_table_id == web_table.id).delete(
            synchronize_session=False)
        if web_table.status != "cleaned":
            skipped.append(web_table.name)
            db.commit()
            continue
        rows = _master_rows(db, web_table)
        bulk_insert(db.connection(), WebMaster, rows)
        db.commit()
        results.append({"web_table_id": web_table.key, "name": web_table.name, "rows": len(rows)})
    return {"table": S.MASTER, "survey_id": survey_id, "rows": sum(r["rows"] for r in results),
            "tables": results, "skipped": skipped}


# Reads -----------------------------------------------------------------------
def _counts(db: Session, model, column, ids: list[int]) -> dict[int, int]:
    if not ids:
        return {}
    query = db.query(model.web_table_id, func.count(column)).filter(model.web_table_id.in_(ids))
    return dict(query.group_by(model.web_table_id).all())


def _matched_counts(db: Session, ids: list[int], with_year: bool) -> dict[int, int]:
    """Clean rows per table that matched a district (and, if asked, have a year)."""
    if not ids:
        return {}
    query = db.query(WebClean.web_table_id, func.count(WebClean.id)).filter(
        WebClean.web_table_id.in_(ids), WebClean.matched.is_(True))
    if with_year:
        query = query.filter(WebClean.year.is_not(None))
    return dict(query.group_by(WebClean.web_table_id).all())


def _reason(table: WebTable, matched: int, with_year: int) -> str | None:
    if not table.place_column:
        return "no district column"
    if table.status != "cleaned":
        return "not cleaned"
    if not matched:
        return "no district matched"
    if not with_year:
        return "no year recognised"
    return None


def list_tables(db: Session, survey_id: int) -> list[dict]:
    """A survey's Website tables, for the Fetched Data rows and the Merge tick boxes."""
    tables = db.query(WebTable).filter(WebTable.survey_id == survey_id).order_by(WebTable.id).all()
    ids = [t.id for t in tables]
    values = _counts(db, WebRaw, WebRaw.id, ids)
    rows = _counts(db, WebRaw, func.distinct(WebRaw.row_number), ids)
    matched = _matched_counts(db, ids, with_year=False)
    with_year = _matched_counts(db, ids, with_year=True)
    out = []
    for t in tables:
        reason = _reason(t, matched.get(t.id, 0), with_year.get(t.id, 0))
        out.append({
            "web_table_id": t.key, "name": t.name, "page_url": t.page_url,
            "state_name": t.state_name, "state_column": t.state_column,
            "year": t.year, "year_column": t.year_column,
            "indicator_column": t.indicator_column, "status": t.status,
            "rows": rows.get(t.id, 0), "values": values.get(t.id, 0),
            "match_summary": t.match_summary, "mergeable": reason is None, "reason": reason,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        })
    return out


_MODELS = {S.RAW: WebRaw, S.CLEAN: WebClean, S.MASTER: WebMaster}


def summary(db: Session, survey_id: int | None = None) -> dict:
    """Row counts per layer, plus how many Website tables there are."""
    out: dict = {}
    for layer, model in _MODELS.items():
        query = db.query(func.count(model.id))
        if survey_id is not None:
            query = query.filter(model.survey_id == survey_id)
        out[layer] = {"rows": int(query.scalar() or 0), "survey_id": survey_id}
    tables = db.query(func.count(WebTable.id))
    if survey_id is not None:
        tables = tables.filter(WebTable.survey_id == survey_id)
    out["tables"] = int(tables.scalar() or 0)
    return out


def layer_ids_for_datasets(db: Session, dataset_ids: list[str]) -> list[int]:
    """The web_tables ids behind these record cards (what a layer row is scoped by)."""
    if not dataset_ids:
        return []
    return [i for (i,) in db.query(WebTable.id).filter(WebTable.dataset_id.in_(dataset_ids))]


def export_layer(db: Session, layer: str, out_dir: Path | None = None,
                 dataset_ids: list[str] | None = None, survey_id: int | None = None) -> dict:
    """One layer as CSV (UTF-8 with BOM, so Excel shows Indian place names)."""
    if layer not in _MODELS:
        raise ValueError(f"Unknown layer '{layer}'. Use one of: {', '.join(S.LAYERS)}")
    model = _MODELS[layer]
    query = select(model.__table__)
    if survey_id is not None:
        query = query.where(model.survey_id == survey_id)
    if dataset_ids:
        query = query.where(model.web_table_id.in_(layer_ids_for_datasets(db, dataset_ids)))
    frame = pd.read_sql_query(query.order_by(model.id), db.connection())
    out = Path(out_dir) if out_dir else EXPORTS_DIR / "web" / layer
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{layer}__survey_{survey_id if survey_id is not None else 'all'}.csv"
    path.write_bytes(frame.to_csv(index=False).encode("utf-8-sig"))
    return {"path": str(path), "rows": int(frame.shape[0])}
