"""Shape of the merge output layer, in the same form as the warehouse schemas.

A sibling of census_warehouse.schema and hmis_warehouse.schema so the shared
machinery — dataset stamping, row reading, survey replacement — treats
dashboard_master exactly as it treats census_raw, rather than special-casing it.
"""
from app.modules.census_warehouse import schema as census_schema

# The table the dashboard reads. Fixed rather than per-survey: a dashboard
# points at one table and filters by survey_id.
DASHBOARD_TABLE = "dashboard_master"

# One layer, but the row reader takes an allow-list of layer names — naming it
# here keeps an arbitrary table name from reaching SQL as an identifier.
LAYERS = (DASHBOARD_TABLE,)

# The same meta columns every warehouse layer carries, so a merge dataset can
# be read back out of the shared table and the reader can scope and sort it.
DATASET_COL = census_schema.DATASET_COL
INGESTED_COL = census_schema.INGESTED_COL
SURVEY_COL = census_schema.SURVEY_COL
META_COLUMNS = (DATASET_COL, INGESTED_COL, SURVEY_COL)

# `connector` on the Dataset row, which is what tells the two kinds of run
# apart: both write this one table, so the stamp is the only way to ask which
# rows a merge produced and which a re-clean rewrote.
MERGE_CONNECTOR = "merge"
CLEAN_CONNECTOR = "merge_clean"
CONNECTORS = (MERGE_CONNECTOR, CLEAN_CONNECTOR)


def is_layer_table(table_name: str | None) -> bool:
    return table_name in LAYERS
