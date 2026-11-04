"""Plain values passed between the scan stages (discovery -> scoring -> store)."""
from dataclasses import dataclass, field

import pandas as pd


@dataclass
class TableItem:
    # html_table | embedded | file_table | pdf_table | docx_table
    kind: str
    title: str
    locator: dict
    df: pd.DataFrame


@dataclass
class TextItem:
    title: str
    source: str  # "page", or the file's URL
    text: str


@dataclass
class FileExtract:
    description: str  # e.g. "Excel, 3 sheets → 2 tables"
    tables: list[TableItem] = field(default_factory=list)
    texts: list[TextItem] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)  # scanned | zip_truncated | timeout
    # Bytes unpacked from a ZIP, counted against the scan's total budget.
    unpacked_bytes: int = 0
