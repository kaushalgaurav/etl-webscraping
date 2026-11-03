"""Background job table.

A national Census pull is ~33 paginated calls and can run for minutes; that
cannot be held open as an HTTP request. Endpoints enqueue a Job and return its
id immediately. Kept in SQLite at this scale, but shaped so it can move to a
real queue (Redis/Celery) without changing callers.
"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.common import new_id, utcnow


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=new_id)
    job_type: Mapped[str] = mapped_column(String(32))  # source_run | source_run_all
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued|running|succeeded|failed
    source_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    dataset_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Human-readable progress, updated as the job runs ("page 12/33, 60000 rows").
    progress: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
