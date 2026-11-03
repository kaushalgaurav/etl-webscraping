"""Canonical shape of the census warehouse tables.

Every census fetch, at any level, lands in the same three tables rather than
its own `ds_<id>`:

    census_raw     append-only, exactly what the connector returned
    census_clean   census_raw put through the standard cleaning operations
    census_master  one row per area — the deduped, final figures

The levels do not share a grain: a district row has no subdistrict, a
village/town row has four extra geography columns. Rather than a table per
level, the union of the geography columns is kept and the ones that do not
apply at a level are left NULL. `level` says which grain a row is at, and
`GRAIN` says which columns identify a row at that level.
"""

RAW = "census_raw"
CLEAN = "census_clean"
MASTER = "census_master"
LAYERS = (RAW, CLEAN, MASTER)

# Stamped on every row so a shared table can still be read back as one
# dataset, and so master can pick the most recent row for an area.
DATASET_COL = "_dataset_id"
INGESTED_COL = "_ingested_at"
# Which survey a row belongs to. Re-running a survey replaces its own rows
# and leaves every other survey in the layer untouched.
SURVEY_COL = "survey_id"
LEVEL_COL = "level"
META_COLUMNS = (DATASET_COL, INGESTED_COL, SURVEY_COL, LEVEL_COL)

# Geography, coarse to fine. Ordered so the warehouse tables read left to
# right as a hierarchy.
GEO_COLUMNS = (
    "state",
    "state_name",
    "district",
    "district_name",
    "subdistrict",
    "town_village",
    "tv_seq_no",
    "urbrur",
    "is_duplicate_area",
)

# Which geography columns actually identify a row at each level. Anything
# finer is NULL there, so it must not take part in the key — otherwise every
# district row would look distinct from every other.
GRAIN = {
    "district": ("state", "district"),
    "subdistrict": ("state", "district", "subdistrict"),
    "village_town": ("state", "district", "subdistrict", "town_village", "urbrur"),
    "ward": ("state", "district", "subdistrict", "town_village", "urbrur"),
}

# The widest key any level uses; master dedupes on this plus `level`, and the
# columns a level does not use are NULL for its rows, which compares equal.
MASTER_KEY = (LEVEL_COL,) + GRAIN["village_town"]


def is_layer_table(table_name: str | None) -> bool:
    """True for the shared census tables, False for per-dataset `ds_<id>`."""
    return table_name in LAYERS
