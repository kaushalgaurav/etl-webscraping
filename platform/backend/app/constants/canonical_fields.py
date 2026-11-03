"""Semantic dictionary: canonical field names and their aliases.

The single place where "these column names mean the same thing" knowledge
lives. Extend as new domains arrive — no code changes needed elsewhere.
"""

CANONICAL_FIELDS: dict[str, list[str]] = {
    # Crosswalk ids first: they are the only keys that genuinely identify the
    # same place across sources. Names and each source's own codes do not
    # agree — the census calls Andaman & Nicobar "35", HMIS spells it out.
    "unique_id": ["unique_id", "district_unique_id", "area_id"],
    "district_id": ["district_id", "dist_id"],
    "state_id": ["state_id", "st_id"],
    # NFHS-5 factsheets use State_UT / District_Names; Census uses state_name /
    # district_name — both must resolve to the same canonical field to merge.
    "state": [
        "state", "state_ut", "state_name", "states", "statename", "st_name",
        "province", "state_uts", "state_union_territory",
    ],
    "district": [
        "district", "district_name", "district_names", "dist_name",
        "districtname", "distt", "district_nm",
    ],
    "block": ["block", "blocks", "no_of_blocks", "cd_block", "block_name"],
    "year": ["year", "yr", "financial_year", "fiscal_year", "fy", "reference_year"],
    "population": ["population", "pop", "total_population", "total_pop", "population_count"],
    "male_population": ["male_population", "males", "male_pop", "population_male"],
    "female_population": ["female_population", "females", "female_pop", "population_female"],
    "literacy_rate": ["literacy_rate", "literacy", "literacy_pct", "literacy_percentage"],
    "doctors": ["doctors", "total_doctors", "no_of_doctors", "physicians", "allopathic_doctors"],
    "hospitals": ["hospitals", "total_hospitals", "no_of_hospitals", "hospital_count"],
    "indicator": ["indicator", "indicator_name", "kpi", "metric_name"],
    "value": ["value", "val", "amount", "count", "figure"],
}

# Columns that make sense as join keys (as opposed to measures).
# Ordered by how reliably each identifies a place, because merge takes the
# first that both sides carry. A crosswalk id matches exactly; "state" only
# matches when both sides spell or code it the same way, which they rarely do.
KEY_LIKE_FIELDS = ("unique_id", "district_id", "state_id",
                   "state", "district", "block", "year")

CANONICAL_THRESHOLD = 0.72  # min ratio to accept a fuzzy canonical match
DIRECT_FUZZY_THRESHOLD = 0.60  # min ratio for raw-name matching
DIRECT_FUZZY_CAP = 0.80  # raw-name matching never claims more confidence than this
