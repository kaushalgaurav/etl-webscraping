"""Generic REST API connector.

Fetches JSON from an arbitrary endpoint and flattens it into a table. Since
APIs vary, this uses a small heuristic: look for the first top-level list
(directly, or under a common wrapper key like "records"/"data"/"results"/
"items"), and json_normalize that. Falls back to normalizing the whole
payload as a single-row table if no list is found.
"""
import requests

import pandas as pd

from app.core.config import settings
from app.modules.ingestion.connectors.base import ConnectorError, IngestResult

WRAPPER_KEYS = ("records", "data", "results", "items", "rows", "value")


def find_records(payload: object) -> list[dict] | None:
    if isinstance(payload, list):
        return payload if payload and isinstance(payload[0], dict) else None
    if isinstance(payload, dict):
        for key in WRAPPER_KEYS:
            value = payload.get(key)
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value
    return None


def ingest_url(url: str, headers: dict | None = None) -> IngestResult:
    try:
        resp = requests.get(url, headers=headers, timeout=settings.request_timeout_seconds)
        resp.raise_for_status()
        payload = resp.json()
    except requests.RequestException as exc:
        raise ConnectorError(f"Request to '{url}' failed: {exc}") from exc
    except ValueError as exc:
        raise ConnectorError(f"Response from '{url}' is not valid JSON: {exc}") from exc

    records = find_records(payload)
    warnings: list[str] = []
    if records is not None:
        df = pd.json_normalize(records)
    else:
        warnings.append(
            "No list of records found in the JSON payload; normalized the "
            "whole document as a single row."
        )
        df = pd.json_normalize(payload)

    if df.empty:
        raise ConnectorError(f"'{url}' produced an empty table.")

    return IngestResult(
        dataframe=df,
        connector="rest_api",
        source_ref=url,
        raw_bytes=resp.content,
        raw_filename="response.json",
        warnings=warnings,
    )
