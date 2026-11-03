"""Export naming: keep each acquisition method in its own file.

Data pulled from an API, scraped from a web page, and downloaded/uploaded as a
file are different provenance even when the columns look alike — mixing them
into one output hides where a number came from. So every export is written to
its own CSV, named and foldered by the method that produced it.

    api/      rest_api, data_gov_in, data_gov_catalog, census_india, hmis_reports
    web/      html_table, dynamic_site
    file/     csv_excel  (upload or direct download link)
    merged/   merge, merge_clean  (a master built from several of the above)

The category alone cannot say *which* source a file came from — census and
HMIS are both programmatic pulls, so both land in api/. The connector is
therefore part of the filename too:

    api__census_india__census_districts__a1b2c3.csv
    api__hmis_reports__hmis_f_1_2019_2020__d4e5f6.csv

so the source is readable without opening the file or looking up the id.
"""
import re

CONNECTOR_CATEGORY = {
    "rest_api": "api",
    "data_gov_in": "api",
    "data_gov_catalog": "api",
    "census_india": "api",
    # The reports arrive as .xls, but the pull is programmatic against the
    # HMIS portal's own listing endpoint — the same kind of acquisition as
    # the Census API, not a file someone uploaded.
    "hmis_reports": "api",
    "html_table": "web",
    "dynamic_site": "web",
    "csv_excel": "file",
    "merge": "merged",
    # A re-clean rewrites the merged table in place; its output is the same
    # kind of artefact as the merge that produced it.
    "merge_clean": "merged",
}

# Short, stable names for the filename. Falls back to the connector itself,
# so a connector added later is still identifiable without touching this.
CONNECTOR_SLUGS = {
    "census_india": "census",
    "hmis_reports": "hmis",
    "data_gov_in": "datagov",
    "data_gov_catalog": "datagov",
    "merge_clean": "merge_clean",
}

CATEGORY_LABELS = {
    "api": "Pulled from an API",
    "web": "Parsed from a web page",
    "file": "Downloaded or uploaded file",
    "merged": "Merged master dataset",
    "other": "Uncategorized",
}


def category_for(connector: str) -> str:
    return CONNECTOR_CATEGORY.get(connector, "other")


def safe_slug(text: str, max_length: int = 60) -> str:
    """Filesystem-safe fragment of a dataset name."""
    slug = re.sub(r"[^0-9A-Za-z]+", "_", str(text)).strip("_")
    return (slug[:max_length].strip("_") or "dataset").lower()


def source_slug(connector: str) -> str:
    """Short name of the source, for the filename."""
    return CONNECTOR_SLUGS.get(connector, safe_slug(connector, 20))


def export_filename(dataset, extension: str = "csv") -> str:
    """`<category>__<source>__<name>__<id>.csv`.

    Both halves of the provenance are in the name: *how* it was acquired
    (the category, which is also the folder) and *what it came from* (the
    connector). Without the second, census and HMIS exports are both `api__…`
    and tell you nothing apart.
    """
    category = category_for(dataset.connector)
    return (
        f"{category}__{source_slug(dataset.connector)}"
        f"__{safe_slug(dataset.name)}__{dataset.id}.{extension}"
    )
