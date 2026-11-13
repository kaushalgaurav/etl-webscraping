"""The staging folder: what a Search found, held only until Save or expiry.

    <scrape_dir>/<scan_id>/tables/<finding_id>.parquet   each table, full
    <scrape_dir>/<scan_id>/files/<nnn>_<name>            each downloaded file
    <scrape_dir>/<scan_id>/texts/<finding_id>.txt        each text finding

Lives outside data/raw so cleanup.purge_all() never deletes a scan under
review. Every path read back goes through resolve(), which refuses anything
outside the scan's own folder.
"""
import shutil
from pathlib import Path

import pandas as pd

from app.core.config import settings
from app.crud.dataset import safe_column_names
from app.modules.ingestion.connectors.hmis_reports import safe_filename


def scan_dir(scan_id: str) -> Path:
    return Path(settings.scrape_dir) / scan_id


def resolve(scan_id: str, rel: str) -> Path:
    root = Path(settings.scrape_dir).resolve()
    base = (root / scan_id).resolve()
    path = (base / rel).resolve()
    if base.parent != root or base not in path.parents:
        raise ValueError(f"'{rel}' is outside scan '{scan_id}'.")
    return path


def _write(scan_id: str, rel: str) -> Path:
    path = resolve(scan_id, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _as_text(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(value)


def prepare_for_parquet(df: pd.DataFrame) -> pd.DataFrame:
    """Column names safe for Parquet and SQL; mixed-type columns as text.

    A column holding 1, "NA" and "1,234" cannot be written as one Parquet
    type; as text it keeps every value exactly as the site showed it, and the
    cleaning stage converts numbers after import as it does for any upload.
    """
    out = safe_column_names(df.reset_index(drop=True))
    for col in out.columns:
        if out[col].dtype == object:
            out[col] = out[col].map(_as_text).astype(object)
    return out


def write_table(scan_id: str, finding_id: str, df: pd.DataFrame) -> str:
    rel = f"tables/{finding_id}.parquet"
    df.to_parquet(_write(scan_id, rel), index=False)
    return rel


def read_table(scan_id: str, rel: str) -> pd.DataFrame:
    return pd.read_parquet(resolve(scan_id, rel))


def write_bytes(scan_id: str, position: int, filename: str, content: bytes) -> str:
    rel = f"files/{position:03d}_{safe_filename(filename)}"
    _write(scan_id, rel).write_bytes(content)
    return rel


def write_text(scan_id: str, finding_id: str, text: str) -> str:
    rel = f"texts/{finding_id}.txt"
    _write(scan_id, rel).write_text(text, encoding="utf-8")
    return rel


def delete_scan_dir(scan_id: str) -> None:
    shutil.rmtree(scan_dir(scan_id), ignore_errors=True)
