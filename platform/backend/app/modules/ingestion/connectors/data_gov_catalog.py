"""data.gov.in *catalog* connector — many resources, one dataset.

Some datasets aren't published as one resource. HMIS is the extreme case: it
is one catalog per state, each holding one resource *per district per month*
("Item-wise report for Dehradun of Uttarakhand upto April-2020-21"). Fetching
it means enumerating a catalog's resources and stacking them.

This connector does that:
    catalog uuid -> list resources (catalog API) -> fetch each (resource API)
                 -> concat, tagging every row with the resource it came from

Two deliberate behaviours:

* **Partial results beat no results.** The resource API rate-limits hard on
  the shared sample key. If it starts refusing partway, the connector returns
  what it already has with a warning, rather than throwing away good pages.
* **Provenance per row.** Each row carries `_resource_title` / `_resource_id`
  so a merged figure can be traced back to the exact monthly report it came
  from (see data lineage in the README).
"""
import time
from collections.abc import Callable

import pandas as pd
import requests

from app.modules.ingestion.connectors import data_gov_in
from app.modules.ingestion.connectors.base import ConnectorError, IngestResult

CATALOG_RESOURCES_API = "https://www.data.gov.in/backend/dmspublic/v1/resources"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; DataIngestionEngine/1.0)"}
LIST_PAGE_SIZE = 100
REQUEST_TIMEOUT = 90
# Be polite between resource fetches — this is a government portal and the
# shared key's quota is easily exhausted.
DELAY_BETWEEN_RESOURCES = 0.5


def _first(value):
    """Catalog API returns some fields as single-element lists."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def list_catalog_resources(catalog_uuid: str, max_resources: int | None = None) -> list[dict]:
    """Enumerate every resource belonging to a catalog."""
    resources: list[dict] = []
    offset = 0

    while True:
        params = {
            "filters[catalog_uuid]": catalog_uuid,
            "limit": LIST_PAGE_SIZE,
            "offset": offset,
            "sort[created]": "desc",
        }
        try:
            resp = requests.get(
                CATALOG_RESOURCES_API, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT
            )
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise ConnectorError(
                f"Could not list resources for catalog '{catalog_uuid}': {exc}"
            ) from exc

        rows = (payload.get("data") or {}).get("rows") or []
        if not rows:
            break

        for row in rows:
            resources.append({
                "uuid": _first(row.get("uuid")),
                "title": _first(row.get("title")),
                "catalog_title": _first(row.get("catalog_title")),
            })
            if max_resources and len(resources) >= max_resources:
                return resources

        offset += len(rows)
        total = payload.get("total")
        if total is not None and offset >= total:
            break

    return resources


def ingest(
    catalog_uuid: str,
    max_resources: int | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> IngestResult:
    """Fetch and stack every resource in a data.gov.in catalog.

    Args:
        catalog_uuid: the catalog to enumerate.
        max_resources: cap the number of resources fetched. Strongly advised
            for HMIS-style catalogs, which can hold hundreds.
    """
    resources = list_catalog_resources(catalog_uuid, max_resources=max_resources)
    if not resources:
        raise ConnectorError(f"Catalog '{catalog_uuid}' contains no resources.")

    frames: list[pd.DataFrame] = []
    warnings: list[str] = []
    failed = 0

    for index, resource in enumerate(resources, start=1):
        resource_id = resource["uuid"]
        if not resource_id:
            continue

        if progress_callback:
            progress_callback(
                f"Resource {index}/{len(resources)}: {str(resource['title'])[:60]}"
            )

        try:
            result = data_gov_in.ingest(resource_id)
        except ConnectorError as exc:
            failed += 1
            message = str(exc)
            # Rate limiting won't clear by pressing on — stop and keep what we have.
            if "429" in message or "rate-limited" in message.lower():
                warnings.append(
                    f"Stopped after {index - 1}/{len(resources)} resources: "
                    f"data.gov.in rate-limited the request. Partial data returned. ({message})"
                )
                break
            warnings.append(f"Skipped '{resource['title']}': {message[:160]}")
            continue

        df = result.dataframe
        df["_resource_title"] = resource["title"]
        df["_resource_id"] = resource_id
        frames.append(df)
        time.sleep(DELAY_BETWEEN_RESOURCES)

    if not frames:
        raise ConnectorError(
            f"Catalog '{catalog_uuid}': all {len(resources)} resource fetch(es) failed. "
            + (warnings[0] if warnings else "")
        )

    combined = pd.concat(frames, ignore_index=True, sort=False)
    if failed:
        warnings.append(f"{failed} of {len(resources)} resources could not be fetched.")

    return IngestResult(
        dataframe=combined,
        connector="data_gov_catalog",
        source_ref=f"catalog:{catalog_uuid} ({len(frames)}/{len(resources)} resources)",
        warnings=warnings,
    )
