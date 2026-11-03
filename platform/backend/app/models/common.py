"""Shared model helpers."""
import uuid
from datetime import datetime, timezone


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def dataset_table_name(dataset_id: str) -> str:
    """Name of the SQL table holding a dataset's rows."""
    return f"ds_{dataset_id}"
