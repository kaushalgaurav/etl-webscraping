"""Save: put the admin's ticked tables into the Website warehouse.

Each ticked table becomes one `web_tables` row (per year) plus its values in
`web_raw`, exactly as found, one value per row. Cleaning is the admin's next
step (the Clean button), as for Census and HMIS. Each Save also gets a record
card in `datasets` (stage raw, table web_raw), the unit of provenance.

Saving the same table again (same survey, page, place on the page and year)
replaces that year's rows; another year is added beside it. Then everything
not ticked is deleted: the staging folder and the unpicked finding rows. The
scan row and the picked findings stay as provenance.

One Save is one claim: claim_for_import flips the scan from done to
importing in a single conditional UPDATE, so a double click gets a 409
instead of a second set of rows. Spec: 2026-11-05-website-warehouse-design §5.1.
"""
from collections.abc import Callable

import pandas as pd
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.constants import district_crosswalk
from app.crud import dataset as dataset_crud
from app.models import Dataset, Profile, ScrapeFinding, ScrapeScan, WebClean, WebMaster, WebRaw, WebTable
from app.models.web_base import bulk_insert
from app.modules.scraping import store
from app.modules.scraping.service import (
    TABLE_KINDS,
    ScanConflict,
    ScanRequestError,
    default_name,
    find_finding,
    get_scan,
    has_saved_tables,
)
from app.modules.web_warehouse import schema as web_schema
from app.modules.web_warehouse.years import financial_years

# datasets.name is VARCHAR(255).
MAX_NAME = 240

CONNECTOR_FOR_KIND = {
    "html_table": "web_table",
    "rendered_table": "web_table",
    "feed": "web_feed",
    "embedded": "web_table",
    "file_table": "web_file",
    "pdf_table": "web_file",
    "docx_table": "web_file",
}

YEAR_MESSAGE = "Choose a year, or the column that holds the year."
INDICATOR_MESSAGE = "The indicator column can't also be the district, state or year column."


def _guess(finding: ScrapeFinding) -> dict:
    """The review screen's district guess, only when it found districts."""
    joinable = (finding.signals or {}).get("joinable") or {}
    return joinable if joinable.get("level") == "district" else {}


def place_column(finding: ScrapeFinding, item: dict) -> str | None:
    return item.get("place_column") or _guess(finding).get("place_column")


def state_column(finding: ScrapeFinding, item: dict) -> str | None:
    if item.get("state_name"):
        return None
    return item.get("state_column") or _guess(finding).get("state_column")


def value_columns(finding: ScrapeFinding, item: dict) -> list[str]:
    """The kept columns (all when none are ticked) minus place, state, year and indicator."""
    own = {place_column(finding, item), state_column(finding, item), item.get("year_column"),
           item.get("indicator_column")}
    kept = item.get("columns") or list(finding.column_names or [])
    return [c for c in kept if c not in own]


def _check_item(finding: ScrapeFinding, item: dict) -> None:
    if bool(item.get("year")) == bool(item.get("year_column")):
        raise ScanRequestError(YEAR_MESSAGE)
    if item.get("year") and item["year"] not in financial_years():
        raise ScanRequestError("Year must be a financial year like 2019-2020.")
    named = [item.get("place_column"), item.get("state_column"), item.get("year_column"),
             item.get("indicator_column"), *(item.get("columns") or [])]
    known = set(finding.column_names or [])
    for column in named:
        if column and column not in known:
            raise ScanRequestError(f"Column '{column}' is not in this table.")
    if place_column(finding, item) and not item.get("state_name") \
            and not state_column(finding, item):
        raise ScanRequestError("Choose the state, or the column that holds it.")
    if item.get("state_name") and district_crosswalk.lookup_state(item["state_name"]) is None:
        raise ScanRequestError(f"Unknown state '{item['state_name']}'.")
    indicator = item.get("indicator_column")
    if indicator and indicator in {place_column(finding, item), state_column(finding, item),
                                   item.get("year_column")}:
        raise ScanRequestError(INDICATOR_MESSAGE)


def claim_for_import(db: Session, scan_id: str, items: list[dict]) -> None:
    scan = get_scan(db, scan_id)
    if not items:
        raise ScanRequestError("Pick at least one table to save.")
    ids = [item["finding_id"] for item in items]
    if len(set(ids)) != len(ids):
        raise ScanRequestError("The same table was picked twice.")
    for item in items:
        finding_id = item["finding_id"]
        finding = find_finding(db, finding_id)
        if finding is None or finding.scan_id != scan.id:
            raise ScanRequestError(f"Finding '{finding_id}' is not part of scan '{scan.key}'.")
        if finding.kind not in TABLE_KINDS:
            raise ScanRequestError(f"Finding '{finding_id}' is not a table; only tables can be saved.")
        if finding.imported_dataset_id:
            raise ScanConflict(
                f"Finding '{finding_id}' was already saved as dataset '{finding.imported_dataset_id}'.")
        _check_item(finding, item)
    claimed = db.execute(
        update(ScrapeScan)
        .where(ScrapeScan.id == scan.id, ScrapeScan.status == "done")
        .values(status="importing")
    ).rowcount
    db.commit()
    if claimed != 1:
        db.refresh(scan)
        raise ScanConflict(f"This search is {scan.status}; it can be saved only once it is done.")


def _source_ref(scan: ScrapeScan, finding: ScrapeFinding) -> str:
    locator = finding.locator or {}
    base = locator.get("file_url") or locator.get("feed_url") or scan.final_url or scan.url
    extra = ", ".join(f"{k}={v}" for k, v in locator.items()
                      if k not in ("source", "file_url", "feed_url"))
    return f"{base} [{extra}]" if extra else base


def _text(value) -> str | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(value)


def _same_table(db: Session, survey_id: int, page_url: str, locator: dict,
                year: str | None) -> WebTable | None:
    candidates = db.query(WebTable).filter(
        WebTable.survey_id == survey_id, WebTable.page_url == page_url,
        WebTable.year.is_(None) if year is None else WebTable.year == year)
    return next((t for t in candidates if (t.source_locator or {}) == (locator or {})), None)


def _clear_rows(db: Session, table: WebTable) -> None:
    for model in (WebMaster, WebClean, WebRaw):
        db.query(model).filter(model.web_table_id == table.id).delete(synchronize_session=False)


def _value_name(indicator: str | None, column: str, several: bool) -> str:
    """What a value is filed under: its heading, or in a long table its row's
    indicator (plus the heading when several value columns are kept)."""
    if indicator is None:
        return column
    return f"{indicator} · {column}" if several else indicator


def _raw_rows(df: pd.DataFrame, table: WebTable, survey_id: int, values: list[str],
              place: str | None, state: str | None, year_col: str | None,
              indicator_col: str | None = None) -> list[dict]:
    rows = []
    for number, record in enumerate(df.to_dict(orient="records"), start=1):
        indicator = (_text(record.get(indicator_col)) or "").strip() if indicator_col else None
        if indicator_col and not indicator:
            continue  # nothing to file this row's values under
        common = {
            "survey_id": survey_id, "web_table_id": table.id, "row_number": number,
            "place_text": _text(record.get(place)) if place else None,
            "state_text": _text(record.get(state)) if state else None,
            "year_text": _text(record.get(year_col)) if year_col else None,
        }
        for column in values:
            rows.append({**common, "column_name": _value_name(indicator, column, len(values) > 1),
                         "value_text": _text(record.get(column))})
    return rows


def _save_one(db: Session, scan: ScrapeScan, finding: ScrapeFinding, item: dict,
              survey_id: int) -> dict:
    name = ((item.get("name") or "").strip() or default_name(scan, finding))[:MAX_NAME]
    page_url = scan.final_url or scan.url
    year = item.get("year") or None
    place, state = place_column(finding, item), state_column(finding, item)
    values = value_columns(finding, item)

    table = _same_table(db, survey_id, page_url, finding.locator, year)
    replaced = table is not None
    old_card = table.dataset_id if replaced else None
    if table is None:
        table = WebTable(survey_id=survey_id, page_url=page_url,
                         source_locator=finding.locator or {}, year=year)
        db.add(table)
    else:
        _clear_rows(db, table)
    table.name = name
    table.finding_id = finding.id
    table.place_column = place
    table.state_name = item.get("state_name") or None
    table.state_column = state
    table.year_column = item.get("year_column") or None
    table.kept_columns = item.get("columns") or None
    table.indicator_column = item.get("indicator_column") or None
    table.status = "saved"
    table.match_summary = None
    db.flush()

    card = dataset_crud.create_dataset_shell(
        db, name=name, stage="raw", connector=CONNECTOR_FOR_KIND[finding.kind],
        source_ref=_source_ref(scan, finding), table_name=web_schema.RAW, survey_id=survey_id)
    df = store.read_table(scan.key, finding.storage_path)
    card.row_count, card.column_count = int(df.shape[0]), int(df.shape[1])
    card.notes = (f"Website scan {scan.key}, finding {finding.key} ({finding.kind}), "
                  f"{finding.rows} rows x {finding.columns} columns, "
                  f"table hash {finding.content_hash}")
    table.dataset_id = card.id
    if old_card:
        # The replaced rows were that card's; a card with no rows would
        # double-count in row totals and lineage.
        db.query(Profile).filter(Profile.dataset_id == old_card).delete(synchronize_session=False)
        db.query(Dataset).filter(Dataset.id == old_card).delete(synchronize_session=False)

    count = bulk_insert(db.connection(), WebRaw,
                        _raw_rows(df, table, survey_id, values, place, state, item.get("year_column"),
                                  item.get("indicator_column")))
    finding.imported_dataset_id = card.id
    db.commit()
    return {"finding_id": finding.key, "web_table_id": table.key, "dataset_id": card.id,
            "rows": int(df.shape[0]), "values": count, "replaced": replaced}


def run_import(db: Session, scan_id: str, survey_id: int, items: list[dict],
               progress: Callable[[str], None] | None = None) -> dict:
    say = progress or (lambda _message: None)
    scan = get_scan(db, scan_id)
    saved = []
    try:
        for i, item in enumerate(items, 1):
            finding = find_finding(db, item["finding_id"])
            saved.append(_save_one(db, scan, finding, item, survey_id))
            say(f"Saved {i}/{len(items)}: {finding.title}")
    except Exception:
        db.rollback()
        scan = get_scan(db, scan_id)
        scan.status = "done"  # what was saved stays saved; the rest can be retried
        if has_saved_tables(db, scan):
            scan.expires_at = None  # provenance for the saved tables: never purged
        db.commit()
        raise

    # Only what the admin picked is kept. A kept table's parent (its file) is
    # not, so the link is cleared first: the foreign key would refuse the
    # delete on Postgres. The table's locator still names the file.
    (db.query(ScrapeFinding)
     .filter(ScrapeFinding.scan_id == scan.id, ScrapeFinding.imported_dataset_id.is_not(None))
     .update({ScrapeFinding.parent_id: None}, synchronize_session=False))
    (db.query(ScrapeFinding)
     .filter(ScrapeFinding.scan_id == scan.id, ScrapeFinding.imported_dataset_id.is_(None))
     .delete(synchronize_session=False))
    scan.status = "imported"
    scan.expires_at = None
    db.commit()
    store.delete_scan_dir(scan.key)
    return {"items": saved}
