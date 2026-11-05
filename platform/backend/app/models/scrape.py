"""Website tab: one row per Search, one row per thing it found.

A scan is a short-lived holding area. Its findings (tables, files, text) are
kept only until the admin saves or the hold expires; after a save, the scan
row and the picked findings stay as small provenance records and everything
else is deleted. The data itself is staged on disk (scraping/store.py), never
in these rows, apart from a capped preview.
"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.web_base import BIGINT_ID, WebTableBase


class ScrapeScan(WebTableBase, Base):
    __tablename__ = "scrape_scans"

    url: Mapped[str] = mapped_column(Text)
    final_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # running | done | failed | importing | imported
    status: Mapped[str] = mapped_column(String(16), default="running")
    job_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # "Couldn't read" entries: {"what", "code", "message", "suggestion"}
    blocked: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # None once saved: an imported scan is provenance and is kept.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ScrapeFinding(WebTableBase, Base):
    __tablename__ = "scrape_findings"

    scan_id: Mapped[int] = mapped_column(BIGINT_ID, ForeignKey("scrape_scans.id"), index=True)
    # Order the findings were made in; the manifest follows it.
    position: Mapped[int] = mapped_column(Integer, default=0)
    # html_table | embedded | file_table | pdf_table | docx_table | text | file
    kind: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(Text)
    # Where it came from: table index, file URL + sheet/page, JSON path.
    locator: Mapped[dict] = mapped_column(JSON, default=dict)
    # The file finding a table or text was extracted from.
    parent_id: Mapped[int | None] = mapped_column(BIGINT_ID, ForeignKey("scrape_findings.id"),
                                                  nullable=True)
    rows: Mapped[int | None] = mapped_column(Integer, nullable=True)
    columns: Mapped[int | None] = mapped_column(Integer, nullable=True)
    column_names: Mapped[list | None] = mapped_column(JSON, nullable=True)
    preview: Mapped[list | None] = mapped_column(JSON, nullable=True)
    signals: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # File / text facts: name, url, content_type, bytes, extracted, snippet...
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    suggested: Mapped[str | None] = mapped_column(String(8), nullable=True)
    storage_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    imported_dataset_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
