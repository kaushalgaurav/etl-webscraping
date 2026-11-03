"""Dataset + Profile tables.

Tabular data itself lives in per-dataset tables named by dataset_table_name();
these rows are the metadata and lineage around them.
"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.common import new_id, utcnow


class Dataset(Base):
    __tablename__ = "datasets"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255))
    # raw -> clean -> master (merged from one or more clean datasets)
    stage: Mapped[str] = mapped_column(String(16))
    # which ingestion connector produced this (rest_api, data_gov_in,
    # census_india, csv_excel, html_table, dynamic_site, merge)
    connector: Mapped[str] = mapped_column(String(32))
    # Which survey this pull belongs to; re-running a survey replaces its rows.
    survey_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    source_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    table_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    column_count: Mapped[int] = mapped_column(Integer, default=0)
    # dataset ids this one was derived from (cleaning source, or merge inputs)
    parent_ids: Mapped[list] = mapped_column(JSON, default=list)
    # What the cleaning/merge stage actually did — the audit trail shown in the UI.
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    profiles: Mapped[list["Profile"]] = relationship(back_populates="dataset")


class Profile(Base):
    __tablename__ = "profiles"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=new_id)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"))
    profile_json: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    dataset: Mapped[Dataset] = relationship(back_populates="profiles")
