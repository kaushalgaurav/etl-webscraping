"""Ingestion request/response models."""
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from app.modules.ingestion.connectors import hmis_reports


class IngestUrlRequest(BaseModel):
    url: str
    connector: str | None = Field(
        default=None,
        description=(
            "Force a connector instead of auto-detecting: rest_api | data_gov_in | "
            "census_india | csv_excel | html_table | dynamic_site"
        ),
    )
    name: str | None = None


class ProbeRequest(BaseModel):
    url: str


class FetchType(str, Enum):
    api = "api"
    csv = "csv"
    html = "html"


class CensusLevel(str, Enum):
    district = "district"
    subdistrict = "subdistrict"
    village_town = "village_town"
    ward = "ward"


class FetchRequest(BaseModel):
    """One request shape for every source type.

    `level`, `state`, `district`, `urbrur` and `verify_ssl` apply only to the
    Census API (a censusindia.gov.in URL with type=api) and are ignored otherwise.
    """

    survey_id: int = Field(
        description="Which survey this pull belongs to. Required. Re-running with the "
                    "same survey_id replaces that survey's rows; a different one adds "
                    "a separate set, so several surveys live side by side in a layer.",
        examples=[1],
    )
    type: FetchType = Field(description="api = JSON/REST API, csv = CSV/Excel file link, html = web page with tables")
    url: str = Field(
        examples=["https://censusindia.gov.in/nada/index.php/api/", "https://hmis.mohfw.gov.in/"],
        description="Census API, HMIS Standard Reports, a CSV/Excel link, or a web page.",
    )
    name: str | None = Field(default=None, description="Dataset name; auto-generated if omitted.")

    level: CensusLevel = Field(
        default=CensusLevel.district,
        description="Census only. district (33 calls, ~1 min) | subdistrict (~300 calls) | "
                    "village_town (~11k calls, hours) | ward (~1.4k calls) for All India.",
    )
    state: int | None = Field(default=None, description="Census only. State code; omit for All India. See GET /census/districts.")
    district: int | None = Field(default=None, description="Census only. District code (requires state).")
    urbrur: Literal[0, 1, 2] | None = Field(
        default=0,
        description="Census only. 0=total, 1=rural, 2=urban, null=all three as rows. "
                    "Below district level there is no total row, so rural+urban are kept.",
    )
    verify_ssl: bool = Field(default=False, description="Census only. The host's cert chain is incomplete.")

    path: str | None = Field(
        default=None,
        description="HMIS only. Folder in the Standard Reports tree; omit for the whole tree. "
                    "e.g. 'Standard Reports/12~H. RCH Reports - State Wise/2014-2015'.",
    )
    max_files_per_chunk: int | None = Field(
        default=None,
        description="HMIS only. Cap the reports taken from each folder — useful for a trial run.",
    )
    file_pattern: str | None = Field(
        default=None,
        description="HMIS only. Case-insensitive glob on the report filename, e.g. 'ALL.xls' "
                    "to take only the combined report from each folder and skip the "
                    "per-theme ones. Omit for every file.",
    )
    crawl: bool = Field(
        default=False,
        description="HMIS only. Plan the named `path` into one job per branch, the way "
                    "omitting `path` plans the whole tree. Ignored without `path`.",
    )
    refresh: bool = Field(
        default=False,
        description="HMIS only. Re-crawl branches already ingested; by default a crawl "
                    "queues only branches (e.g. new years) it has not ingested before.",
    )
    batch_by: Literal["report_type", "year", "period"] = Field(
        default="year",
        description="HMIS only, and only when `path` is omitted. How finely to split the "
                    "tree into separate jobs.",
    )
    report_type: str | None = Field(
        default=hmis_reports.DEFAULT_REPORT_TYPE,
        description="HMIS crawl only. Which report-type folder to crawl; the default is "
                    "the F.1 key-indicators report. The other report types carry "
                    "different indicators in a different layout, so crawling them "
                    "fetches hours of reports nothing reads. Pass null for the whole tree.",
        examples=[hmis_reports.DEFAULT_REPORT_TYPE],
    )
    last_years: int | None = Field(
        default=hmis_reports.DEFAULT_LAST_YEARS,
        ge=1,
        description="HMIS crawl only. Keep only the N most recent report years. "
                    "Pass null for every year back to 2008-2009.",
        examples=[2],
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"survey_id": 1, "type": "api", "url": "https://censusindia.gov.in/nada/index.php/api/", "level": "district"},
                {"survey_id": 2, "type": "api", "url": "https://censusindia.gov.in/nada/index.php/api/", "level": "village_town", "state": 10},
                {"survey_id": 3, "type": "api", "url": "https://hmis.mohfw.gov.in/", "batch_by": "year",
                 "file_pattern": "ALL.xls",
                 "report_type": "9~F.1. Performance of Key HMIS Indicators(upto District Level)",
                 "last_years": 2},
                {"survey_id": 4, "type": "api", "url": "https://hmis.mohfw.gov.in/",
                 "path": "Standard Reports/9~F.1. Performance of Key HMIS Indicators(upto District Level)/2008-2009/MonthUpToDecember/A & N Islands",
                 "file_pattern": "ALL.xls"},
                {"survey_id": 5, "type": "csv", "url": "https://example.com/data.csv"},
                {"survey_id": 6, "type": "html", "url": "https://en.wikipedia.org/wiki/List_of_states_and_union_territories_of_India_by_population"},
            ]
        }
    }
