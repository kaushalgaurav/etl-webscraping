"""Canonical shape of the HMIS warehouse tables.

Mirrors app/modules/census_warehouse: every HMIS report, at any level and
from any year, lands in the same three tables rather than its own `ds_<id>`.

    hmis_raw     append-only, one row per (area, indicator, metric year)
    hmis_clean   typed and standardised
    hmis_master  one row per area/indicator/period — the deduped figures

Unlike the census tables the grain here is long, not wide: the reports name
~200 indicators in prose and pair each with its previous year, so a wide
layout produces a new column per indicator per year (one F.1 year alone gave
1,257). Long form keeps the schema fixed however many years are loaded.
"""

RAW = "hmis_raw"
CLEAN = "hmis_clean"
MASTER = "hmis_master"
# Deduped long rows, kept beside master: master is wide like census_master,
# so it cannot carry which report published each figure.
LAYERS = (RAW, CLEAN, MASTER)

DATASET_COL = "_dataset_id"
INGESTED_COL = "_ingested_at"
# Which survey a row belongs to. Re-running a survey replaces its own rows
# and leaves every other survey in the layer untouched.
SURVEY_COL = "survey_id"
META_COLUMNS = (DATASET_COL, INGESTED_COL, SURVEY_COL)

# Where the figure came from: the report tree's own dimensions.
SOURCE_COLUMNS = ("report_type", "report_year", "period", "parent_area", "source_file")

# What the figure describes.
GEO_COLUMNS = ("level", "area")

# Resolved against app/constants/district_crosswalk.tsv. Names and ids are both
# kept: the name is what the report said, the id is what joins to the census.
GEO_ID_COLUMNS = ("state_name", "state_id", "district_name", "district_id", "unique_id")

# Which indicator, and how it is classified.
INDICATOR_COLUMNS = ("indicator", "variable", "programme", "grouping")

# The measurement itself. `metric_year` is the year the value belongs to,
# which is not the folder year: a 2019-2020 report also carries 2018-19
# figures for comparison.
VALUE_COLUMNS = ("metric_year", "value")

# One row per area, indicator and reporting window. `report_year` is excluded
# deliberately — the same (area, indicator, metric_year, period) appears in
# both the year it was published and the next year's comparison column, and
# those must collapse to one row.
MASTER_KEY = ("level", "parent_area", "area", "variable", "metric_year", "period")

# One master row per place per reporting window; indicators become columns.
WIDE_INDEX = ("level", "state_name", "state_id", "district_name", "district_id",
              "unique_id", "report_year", "period", "metric_year")


def is_layer_table(table_name: str | None) -> bool:
    """True for the shared HMIS tables, False for per-dataset `ds_<id>`."""
    return table_name in LAYERS
