"""Connector registry.

Each acquisition method lives in its own module — API, file download and HTML
parsing never share code paths:

    rest_api.py          generic JSON REST endpoints
    data_gov_in.py       data.gov.in single resource API
    data_gov_catalog.py  data.gov.in whole catalog (many resources, stacked)
    census_india.py      Census of India NADA API
    csv_excel.py         CSV/Excel download or upload
    html_table.py        static HTML table parsing
    dynamic_site.py      headless-browser rendering (stub)
    hmis_reports.py      HMIS Standard Reports file tree (hmis.mohfw.gov.in)
"""
from app.modules.ingestion.connectors import (
    census_india,
    csv_excel,
    data_gov_catalog,
    data_gov_in,
    dynamic_site,
    hmis_reports,
    html_table,
    rest_api,
)

# Human-readable registry, surfaced via GET /connectors.
CONNECTORS = {
    "rest_api": "Generic REST API (JSON)",
    "data_gov_in": "data.gov.in API (single resource)",
    "data_gov_catalog": "data.gov.in catalog (enumerates and stacks all its resources)",
    "census_india": "Census of India (censusindia.gov.in NADA API)",
    "csv_excel": "CSV / Excel (upload or direct link)",
    "html_table": "Static web page (HTML tables)",
    "dynamic_site": "Dynamic website via headless browser (not yet installed)",
    "hmis_reports": "HMIS Standard Reports (hmis.mohfw.gov.in file tree)",
}

# Connectors driven by structured parameters rather than a plain URL.
# The source registry stores those parameters in its `config` field.
PARAMETERIZED_CONNECTORS = {"census_india", "data_gov_catalog", "hmis_reports"}

__all__ = [
    "CONNECTORS",
    "PARAMETERIZED_CONNECTORS",
    "census_india",
    "csv_excel",
    "data_gov_catalog",
    "data_gov_in",
    "dynamic_site",
    "hmis_reports",
    "html_table",
    "rest_api",
]
