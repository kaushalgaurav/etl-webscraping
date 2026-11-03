"""Shared types for every ingestion connector.

Every connector's job is the same: given a source (URL or uploaded file),
produce an IngestResult carrying a tabular pandas DataFrame plus enough
provenance to store the raw payload and label the dataset.
"""
from dataclasses import dataclass, field

import pandas as pd


class ConnectorError(Exception):
    """Raised when a connector cannot ingest the given source."""


@dataclass
class IngestResult:
    dataframe: pd.DataFrame
    connector: str  # rest_api | data_gov_in | csv_excel | html_table | dynamic_site
    source_ref: str  # URL or original filename
    raw_bytes: bytes | None = None
    raw_filename: str | None = None
    warnings: list[str] = field(default_factory=list)
