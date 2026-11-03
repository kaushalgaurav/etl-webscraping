"""Shared DTOs used by more than one module.

Module-specific request/response models live in that module's schemas.py.
"""
from datetime import datetime

from pydantic import BaseModel


class DatasetOut(BaseModel):
    id: str
    name: str
    stage: str
    connector: str
    source_ref: str | None
    row_count: int
    column_count: int
    parent_ids: list[str]
    notes: str | None
    error: str | None
    created_at: datetime

    model_config = {"from_attributes": True}
