"""Names of the Website warehouse tables, in the shape the warehouse router expects.

A sibling of census_warehouse.schema and hmis_warehouse.schema. Unlike those,
the tables are ORM models (app/models/web_warehouse.py) with fixed columns,
one value per row; Merge turns each Website table back into columns.
"""
TABLES = "web_tables"
RAW = "web_raw"
CLEAN = "web_clean"
MASTER = "web_master"
LAYERS = (RAW, CLEAN, MASTER)

SURVEY_COL = "survey_id"
# What the generic row reader scopes a layer by. The web layers carry no
# `_dataset_id`; a dataset's rows are found through web_tables.dataset_id.
DATASET_COL = "web_table_id"
YEAR_COL = "year"

# Every Website column in dashboard_master starts with this, which is how
# the KPI code tells them from `_census` / `_hmis` columns.
COLUMN_PREFIX = "web_"
# Postgres allows 1,600 columns per table, dropped ones included; Merge
# refuses to take dashboard_master past this.
MAX_DASHBOARD_COLUMNS = 1400


def is_layer_table(table_name: str | None) -> bool:
    return table_name in LAYERS
