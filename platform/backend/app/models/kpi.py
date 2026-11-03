"""KPI request table — one row per user KPI prompt and its executed results."""
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.common import new_id, utcnow


class KpiRequest(Base):
    __tablename__ = "kpi_requests"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=new_id)
    # The merge dataset the KPIs were computed from — kept for lineage.
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"))
    # Which survey the request was made against; the scope callers actually use.
    survey_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    prompt: Mapped[str] = mapped_column(Text)
    kpi_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    narrative: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
