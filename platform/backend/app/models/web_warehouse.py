"""Website data as a warehouse: web_tables -> web_raw -> web_clean -> web_master.

One row in web_tables per saved Website table (per year); the three layers
hold one value per row, linked to it by web_table_id. Spec:
docs/superpowers/specs/2026-11-05-website-warehouse-design.md §4.
"""
from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.web_base import BIGINT_ID, WebTableBase


class WebTable(WebTableBase, Base):
    __tablename__ = "web_tables"

    survey_id: Mapped[int] = mapped_column(Integer, index=True)
    finding_id: Mapped[int | None] = mapped_column(
        BIGINT_ID, ForeignKey("scrape_findings.id"), nullable=True)
    # The record card in `datasets` for the newest Save.
    dataset_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    name: Mapped[str] = mapped_column(Text)
    page_url: Mapped[str] = mapped_column(Text)
    source_locator: Mapped[dict] = mapped_column(JSON, default=dict)
    place_column: Mapped[str | None] = mapped_column(Text, nullable=True)
    state_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    state_column: Mapped[str | None] = mapped_column(Text, nullable=True)
    year: Mapped[str | None] = mapped_column(String(9), nullable=True)
    year_column: Mapped[str | None] = mapped_column(Text, nullable=True)
    kept_columns: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Long tables (one row per place per indicator, as NFHS district files):
    # the column naming each row's indicator. Its text becomes the value's name.
    indicator_column: Mapped[str | None] = mapped_column(Text, nullable=True)
    # saved | cleaned
    status: Mapped[str] = mapped_column(String(16), default="saved")
    match_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class WebRaw(WebTableBase, Base):
    __tablename__ = "web_raw"

    survey_id: Mapped[int] = mapped_column(Integer, index=True)
    web_table_id: Mapped[int] = mapped_column(BIGINT_ID, ForeignKey("web_tables.id"), index=True)
    row_number: Mapped[int] = mapped_column(Integer)
    place_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    state_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    year_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    column_name: Mapped[str] = mapped_column(Text)
    value_text: Mapped[str | None] = mapped_column(Text, nullable=True)


class WebClean(WebTableBase, Base):
    __tablename__ = "web_clean"

    survey_id: Mapped[int] = mapped_column(Integer, index=True)
    web_table_id: Mapped[int] = mapped_column(BIGINT_ID, ForeignKey("web_tables.id"), index=True)
    raw_id: Mapped[int | None] = mapped_column(BIGINT_ID, ForeignKey("web_raw.id"), nullable=True)
    state_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    state_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    district_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    district_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unique_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    matched: Mapped[bool] = mapped_column(Boolean, default=False)
    year: Mapped[str | None] = mapped_column(String(9), nullable=True)
    indicator: Mapped[str] = mapped_column(Text)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)


class WebMaster(WebTableBase, Base):
    __tablename__ = "web_master"
    __table_args__ = (
        UniqueConstraint("web_table_id", "unique_id", "year", "indicator", name="uq_web_master_key"),
    )

    survey_id: Mapped[int] = mapped_column(Integer, index=True)
    web_table_id: Mapped[int] = mapped_column(BIGINT_ID, ForeignKey("web_tables.id"), index=True)
    state_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    state_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    district_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    district_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unique_id: Mapped[int] = mapped_column(Integer)
    year: Mapped[str] = mapped_column(String(9))
    indicator: Mapped[str] = mapped_column(Text)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
