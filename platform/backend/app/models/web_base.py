"""The shared base for every Website-flow table, and nothing else.

    id           BIGINT primary key, auto-increment (INTEGER on SQLite, which
                 only auto-increments a plain INTEGER PRIMARY KEY)
    external_id  UUID, unique: the only id the API and the portal ever see
    created_at   set on insert
    updated_at   set on insert, refreshed on every ORM update

The web_raw / web_clean / web_master layers are written in bulk with Core
inserts, where ORM defaults don't run; bulk_insert fills the base columns
itself so those rows carry them too.
"""
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, Uuid, insert
from sqlalchemy.orm import Mapped, mapped_column

from app.models.common import utcnow

BIGINT_ID = BigInteger().with_variant(Integer, "sqlite")


class WebTableBase:
    id: Mapped[int] = mapped_column(BIGINT_ID, primary_key=True, autoincrement=True)
    external_id: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True, nullable=False,
                                                   default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                 default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                 default=utcnow, onupdate=utcnow)

    @property
    def key(self) -> str:
        """The external id as text: what URLs, job payloads and folders use."""
        return str(self.external_id)


def parse_key(key) -> uuid.UUID | None:
    """`key` as a UUID, or None when it isn't one (an unknown id, never a 500)."""
    if isinstance(key, uuid.UUID):
        return key
    try:
        return uuid.UUID(str(key))
    except (ValueError, TypeError, AttributeError):
        return None


def bulk_insert(conn, model: type, rows: list[dict], chunk: int = 5000) -> int:
    """Insert `rows` into `model`'s table, filling the base columns they lack."""
    now = utcnow()
    table = model.__table__
    for start in range(0, len(rows), chunk):
        part = rows[start:start + chunk]
        for row in part:
            row.setdefault("external_id", uuid.uuid4())
            row.setdefault("created_at", now)
            row.setdefault("updated_at", now)
        conn.execute(insert(table), part)
    return len(rows)
