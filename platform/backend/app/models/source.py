"""Source registry table.

The indicators this platform targets live across many portals (Census, NFHS,
HMIS, TB Report, NACO, NCRB, RHS...), each with its own URL or API. One row
here = one fetchable source: which connector reads it, the params or URL it
needs, what geography it's keyed on, and its licence/attribution.
"""
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.common import new_id, utcnow


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255))
    connector: Mapped[str] = mapped_column(String(32))
    # Either {"url": "..."} for URL-driven connectors, or connector-specific
    # params (e.g. {"state": 10, "level": "district"}) for parameterized ones.
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    # What this source is keyed on, so merging knows how to line it up.
    geo_level: Mapped[str | None] = mapped_column(String(32), nullable=True)
    theme: Mapped[str | None] = mapped_column(String(64), nullable=True)
    publisher: Mapped[str | None] = mapped_column(String(255), nullable=True)
    license: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_page: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(16), nullable=True)  # ok | error
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_dataset_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
