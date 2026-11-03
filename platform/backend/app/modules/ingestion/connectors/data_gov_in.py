"""data.gov.in API connector.

data.gov.in resources are fetched as:
    https://api.data.gov.in/resource/<resource-id>?api-key=...&format=json&limit=...
and return {"records": [...], "field": [...], "total": N, ...}. This mirrors
the convention already used in etl/config.py so the same env var and sample
key work for both the legacy ETL and this platform.
"""
import os
import re
import time
from collections.abc import Callable

import pandas as pd
import requests

from app.modules.ingestion.connectors.base import ConnectorError, IngestResult

RESOURCE_API = "https://api.data.gov.in/resource"
# Official public sample key from the data.gov.in docs. Its quota is shared by
# every caller, so it rate-limits (429) almost immediately on real ingests —
# override with DATA_GOV_IN_API_KEY for anything beyond a smoke test.
SAMPLE_API_KEY = "579b464db66ec23bdd000001cdd3946e44ce4aad7209ff7b23ac571b"
API_KEY = os.environ.get("DATA_GOV_IN_API_KEY", SAMPLE_API_KEY)

# data.gov.in is slow on first hit for a large resource — the global 30s
# default times out routinely, so this connector sets its own budget.
REQUEST_TIMEOUT = 120
PAGE_LIMIT = 1000
MAX_RETRIES = 3
BACKOFF_SECONDS = 2

RESOURCE_ID_RE = re.compile(
    r"(?:resource/|resource_id=)([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


def extract_resource_id(url_or_id: str) -> str:
    """Accepts a bare resource UUID or a data.gov.in URL containing one."""
    match = RESOURCE_ID_RE.search(url_or_id)
    if match:
        return match.group(1)
    if re.fullmatch(r"[0-9a-fA-F-]{36}", url_or_id.strip()):
        return url_or_id.strip()
    raise ConnectorError(
        f"Could not find a data.gov.in resource id in '{url_or_id}'."
    )


def _get(api_url: str, params: dict, resource_id: str) -> dict:
    """GET with retry/backoff — this API times out and rate-limits readily."""
    using_sample_key = API_KEY == SAMPLE_API_KEY
    last_error: Exception | None = None

    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(api_url, params=params, timeout=REQUEST_TIMEOUT)

            # Rate limited. Retrying the shared sample key is pointless — the
            # quota is shared across every user of the public docs key.
            if resp.status_code == 429:
                raise ConnectorError(
                    "data.gov.in rate-limited this request (HTTP 429). "
                    + (
                        "You are using the public sample API key, whose quota is shared "
                        "by everyone. Register for a free key at https://data.gov.in and "
                        "set DATA_GOV_IN_API_KEY to ingest full datasets."
                        if using_sample_key
                        else "Wait for the quota window to reset, or reduce PAGE_LIMIT."
                    )
                )
            resp.raise_for_status()
            return resp.json()

        except ConnectorError:
            raise
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < MAX_RETRIES - 1:
                time.sleep(BACKOFF_SECONDS * (2 ** attempt))

    raise ConnectorError(
        f"data.gov.in request failed for resource '{resource_id}' "
        f"after {MAX_RETRIES} attempts: {last_error}"
        + (
            " (Note: the public sample API key is heavily rate-limited — set "
            "DATA_GOV_IN_API_KEY to your own key.)" if using_sample_key else ""
        )
    ) from last_error


def ingest(
    url_or_resource_id: str,
    limit: int | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> IngestResult:
    """Fetch a data.gov.in resource, paginating until all records are retrieved.

    Args:
        limit: stop after this many records (None = fetch everything).
    """
    resource_id = extract_resource_id(url_or_resource_id)
    api_url = f"{RESOURCE_API}/{resource_id}"

    records: list[dict] = []
    offset = 0
    total: int | None = None

    while True:
        page_size = PAGE_LIMIT if limit is None else min(PAGE_LIMIT, limit - len(records))
        payload = _get(
            api_url,
            {"api-key": API_KEY, "format": "json", "limit": page_size, "offset": offset},
            resource_id,
        )

        if total is None:
            total = payload.get("total")
        batch = payload.get("records") or []
        records.extend(batch)
        offset += len(batch)

        if progress_callback and total:
            progress_callback(f"Fetched {len(records):,}/{total:,} records")

        reached_limit = limit is not None and len(records) >= limit
        if not batch or reached_limit or (total is not None and len(records) >= total):
            break

    if not records:
        raise ConnectorError(
            f"data.gov.in resource '{resource_id}' returned no records "
            f"(check the resource id and API key)."
        )

    return IngestResult(
        dataframe=pd.json_normalize(records),
        connector="data_gov_in",
        source_ref=f"{RESOURCE_API}/{resource_id}",
    )
