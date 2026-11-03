"""Source registry request/response models."""
from datetime import datetime

from pydantic import BaseModel, Field


class SourceIn(BaseModel):
    name: str
    connector: str
    config: dict = Field(
        default_factory=dict,
        description=(
            "{'url': ...} for URL connectors, or connector params "
            "e.g. {'state': 10, 'level': 'district'} for parameterized ones."
        ),
    )
    geo_level: str | None = None
    theme: str | None = None
    publisher: str | None = None
    license: str | None = None
    source_page: str | None = None
    enabled: bool = True
    notes: str | None = None


class SourceOut(SourceIn):
    id: str
    last_run_at: datetime | None
    last_status: str | None
    last_error: str | None
    last_dataset_id: str | None
    created_at: datetime

    model_config = {"from_attributes": True}
