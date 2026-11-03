"""Job response model."""
from datetime import datetime

from pydantic import BaseModel


class JobOut(BaseModel):
    id: str
    job_type: str
    status: str
    source_id: str | None
    dataset_id: str | None
    progress: str | None
    result: dict | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    model_config = {"from_attributes": True}
