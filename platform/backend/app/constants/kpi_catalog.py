"""The 227 Key Performance Indicators of General_Population_Template.xlsx.

Two halves, deliberately separated:

* `kpi_catalog.tsv` carries the template's own text — theme, indicator name,
  definition, and the source the template names. It is generated from the
  workbook and is data, not code, so re-reading the template never means
  editing Python.
* `FORMULAS` below says how to actually compute an indicator from
  `dashboard_master`. Only formulas live here, keyed by the TSV's id.

An indicator the data cannot yet answer is simply absent from `FORMULAS`; the
catalog reports it as `unavailable` with the source the template asks for,
rather than guessing a number. That distinction is the point of this module —
a blank cell and a wrong cell are not the same kind of wrong.

Three statuses:
  computed    - the template's named source is what we used.
  proxy       - the template names a survey we have not ingested (usually
                NFHS-5), but HMIS reports a service-based equivalent. The
                value is real and useful, and labelled so nobody mistakes a
                facility-reported rate for a household-survey one.
  unavailable - no source ingested; `reason` names what is missing.
"""
import csv
from functools import lru_cache
from pathlib import Path

CATALOG_PATH = Path(__file__).with_name("kpi_catalog.tsv")

# Column suffixes the merge gives each warehouse's fields.
C = "_census"
H = "_hmis"


def _col(name: str, source: str) -> str:
    return f"{name}{C if source == 'census' else H}"


# kind:
#   column - take a column as-is
#   ratio  - numerator / denominator * scale
#   sum    - add several columns
#
# Every entry also names `basis`: which real source the number comes from.
FORMULAS: dict[str, dict] = {
    # ---------------------------------------------------------------- Demographic
    "demographic__state": {
        "kind": "column", "column": _col("state_name", "census"), "basis": "Census 2011",
    },
    "demographic__population_census_2011": {
        "kind": "column", "column": _col("total_population_both_sexes", "census"),
        "basis": "Census 2011",
    },
    "demographic__male_population_census_2011": {
        "kind": "column", "column": _col("total_population_males", "census"),
        "basis": "Census 2011",
    },
    "demographic__female_population_census_2011": {
        "kind": "column", "column": _col("total_population_females", "census"),
        "basis": "Census 2011",
    },
    "demographic__percentage_males_census_2011": {
        "kind": "ratio", "numerator": _col("total_population_males", "census"),
        "denominator": _col("total_population_both_sexes", "census"),
        "scale": 100, "unit": "%", "basis": "Census 2011",
    },
    "demographic__percentage_females_census_2011": {
        "kind": "ratio", "numerator": _col("total_population_females", "census"),
        "denominator": _col("total_population_both_sexes", "census"),
        "scale": 100, "unit": "%", "basis": "Census 2011",
    },
    "demographic__0_6_years_age_group_population": {
        "kind": "column", "column": _col("persons_aged_0_to_6", "census"),
        "basis": "Census 2011",
    },
    "demographic__mean_household_size_district_level_census_2011": {
        "kind": "ratio", "numerator": _col("total_population_both_sexes", "census"),
        "denominator": _col("number_of_households", "census"),
        "scale": 1, "unit": "persons", "basis": "Census 2011",
    },
    "demographic__total_live_births": {
        "kind": "column", "column": _col("live_births", "hmis"), "basis": "HMIS F.1",
    },
    "demographic__crude_birth_rate_for_1000_population": {
        "kind": "ratio", "numerator": _col("live_births", "hmis"),
        "denominator": _col("total_population_both_sexes", "census"),
        "scale": 1000, "unit": "per 1,000",
        "basis": "HMIS F.1 live births over Census 2011 population",
    },
    "demographic__infant_mortality_rate": {
        "kind": "ratio", "numerator": _col("infant_deaths", "hmis"),
        "denominator": _col("live_births", "hmis"),
        "scale": 1000, "unit": "per 1,000 live births", "basis": "HMIS F.1",
    },
    "demographic__sex_ratio_of_the_total_population_females_per_1_000_males_nfhs_5_distric": {
        "kind": "ratio", "numerator": _col("total_population_females", "census"),
        "denominator": _col("total_population_males", "census"),
        "scale": 1000, "unit": "females per 1,000 males",
        "status": "proxy", "basis": "Census 2011 (template asks for NFHS-5 2019-20)",
    },

    # ------------------------------------------------------------- Socio-economic
    "socio_economic__state": {
        "kind": "column", "column": _col("state_name", "census"), "basis": "Census 2011",
    },
    "socio_economic__district": {
        "kind": "column", "column": _col("district_name", "census"), "basis": "Census 2011",
    },
    "socio_economic__population": {
        "kind": "column", "column": _col("total_population_both_sexes", "census"),
        "basis": "Census 2011",
    },
    # The official Census literacy rate is literates over the population aged
    # 7 and above, not over everyone: including under-7s (who are counted
    # illiterate by definition) understates every district by several points.
    "socio_economic__percentage_of_literates": {
        "kind": "ratio_over_difference",
        "numerator": _col("literate_population_both_sexes", "census"),
        "denominator_minuend": _col("total_population_both_sexes", "census"),
        "denominator_subtrahend": _col("persons_aged_0_to_6", "census"),
        "scale": 100, "unit": "%", "basis": "Census 2011 (age 7+ denominator)",
    },
    "socio_economic__percentage_of_male_literates": {
        "kind": "ratio_over_difference",
        "numerator": _col("literate_population_males", "census"),
        "denominator_minuend": _col("total_population_males", "census"),
        "denominator_subtrahend": _col("males_aged_0_to_6", "census"),
        "scale": 100, "unit": "%", "basis": "Census 2011 (age 7+ denominator)",
    },
    "socio_economic__percentage_of_female_literates": {
        "kind": "ratio_over_difference",
        "numerator": _col("literate_population_females", "census"),
        "denominator_minuend": _col("total_population_females", "census"),
        "denominator_subtrahend": _col("females_aged_0_to_6", "census"),
        "scale": 100, "unit": "%", "basis": "Census 2011 (age 7+ denominator)",
    },
    "socio_economic__worker_population_ratio": {
        "kind": "ratio", "numerator": _col("total_worker_population_both_sexes", "census"),
        "denominator": _col("total_population_both_sexes", "census"),
        "scale": 100, "unit": "%", "basis": "Census 2011",
    },
    "socio_economic__percentage_of_cultivators_to_population": {
        "kind": "ratio", "numerator": _col("main_cultivator_population_both_sexes", "census"),
        "denominator": _col("total_population_both_sexes", "census"),
        "scale": 100, "unit": "%", "basis": "Census 2011 (main cultivators)",
    },
    "socio_economic__number_of_persons_engaged_in_cultivation": {
        "kind": "sum",
        "columns": [_col("main_cultivator_population_both_sexes", "census"),
                    _col("marginal_cultivator_population_both_sexes", "census")],
        "basis": "Census 2011 (main + marginal cultivators)",
    },
    "socio_economic__percentage_of_ag_laborers_to_population": {
        "kind": "ratio",
        "numerator": _col("main_agricultural_labourers_population_both_sexes", "census"),
        "denominator": _col("total_population_both_sexes", "census"),
        "scale": 100, "unit": "%", "basis": "Census 2011 (main agricultural labourers)",
    },
    "socio_economic__number_of_the_population_engaged_in_agriculture": {
        "kind": "sum",
        "columns": [_col("main_cultivator_population_both_sexes", "census"),
                    _col("marginal_cultivator_population_both_sexes", "census"),
                    _col("main_agricultural_labourers_population_both_sexes", "census"),
                    _col("marginal_agricultural_labourers_population_both_sexes", "census")],
        "basis": "Census 2011 (cultivators + agricultural labourers, main and marginal)",
    },
    "socio_economic__percentage_of_hh_industry_to_population": {
        "kind": "ratio",
        "numerator": _col("main_household_industries_population_both_sexes", "census"),
        "denominator": _col("total_population_both_sexes", "census"),
        "scale": 100, "unit": "%", "basis": "Census 2011 (main household industry workers)",
    },
    "socio_economic__percentage_of_marginal_workers_to_population": {
        "kind": "ratio", "numerator": _col("marginal_worker_population_both_sexes", "census"),
        "denominator": _col("total_population_both_sexes", "census"),
        "scale": 100, "unit": "%", "basis": "Census 2011",
    },
    "socio_economic__total_percentage_of_population_employed_ine_other_works": {
        "kind": "ratio", "numerator": _col("main_other_workers_population_both_sexes", "census"),
        "denominator": _col("total_population_both_sexes", "census"),
        "scale": 100, "unit": "%", "basis": "Census 2011 (main other workers)",
    },

    # --------------------------------------------------------------------- RMNCH
    "rmnch__state": {
        "kind": "column", "column": _col("state_name", "census"), "basis": "Census 2011",
    },
    "rmnch__total_number_of_pregnant_women_for_anc": {
        "kind": "column", "column": _col("anc_reg", "hmis"), "basis": "HMIS F.1",
    },
    "rmnch__live_births": {
        "kind": "column", "column": _col("live_births", "hmis"), "basis": "HMIS F.1",
    },
    "rmnch__number_of_still_births": {
        "kind": "column", "column": _col("still_births", "hmis"), "basis": "HMIS F.1",
    },
    "rmnch__still_to_live_births": {
        "kind": "ratio", "numerator": _col("still_births", "hmis"),
        "denominator": _col("live_births", "hmis"),
        "scale": 1000, "unit": "per 1,000 live births", "basis": "HMIS F.1",
    },
    "rmnch__number_of_deliveries_attended_by_non_sba_trained_birth_attended_relative": {
        "kind": "column", "column": _col("home_del_non_sba", "hmis"), "basis": "HMIS F.1",
    },
    "rmnch__adverse_events_following_imunisation_others": {
        "kind": "column", "column": _col("aefi_others", "hmis"), "basis": "HMIS F.1",
    },
    "rmnch__drop_out_between_bcg_measles_mr": {
        "kind": "ratio_of_difference",
        "minuend": _col("bcg", "hmis"), "subtrahend": _col("measles", "hmis"),
        "denominator": _col("bcg", "hmis"),
        "scale": 100, "unit": "%", "basis": "HMIS F.1",
    },
    "rmnch__mothers_who_had_anc_in_first_trimester_nfhs_5_overall_state_and_district": {
        "kind": "column", "column": _col("pct_anc_1st_tri", "hmis"), "unit": "%",
        "status": "proxy", "basis": "HMIS F.1 facility-reported (template asks for NFHS-5)",
    },
    "rmnch__percentage_of_institutional_births_nfhs_5_overall_state_and_district_lev": {
        "kind": "column", "column": _col("pct_inst_del", "hmis"), "unit": "%",
        "status": "proxy", "basis": "HMIS F.1 facility-reported (template asks for NFHS-5)",
    },
    "rmnch__percentage_of_deliveries_attended_by_sba_nfhs_5_overall_state_and_distri": {
        "kind": "column", "column": _col("pct_safe_del", "hmis"), "unit": "%",
        "status": "proxy", "basis": "HMIS F.1 facility-reported (template asks for NFHS-5)",
    },
    "rmnch__births_delivered_by_caesarean_section_nfhs_5_overall_state_and_district_": {
        "kind": "column", "column": _col("pct_csec", "hmis"), "unit": "%",
        "status": "proxy", "basis": "HMIS F.1 facility-reported (template asks for NFHS-5)",
    },

    # ----------------------------------------------------------------- Nutrition
    "nutrition__percentage_of_early_initiation_of_breastfeeding_within_1_hour_of_birth_a": {
        "kind": "column", "column": _col("pct_bf_1hr", "hmis"), "unit": "%",
        "status": "proxy", "basis": "HMIS F.1 facility-reported (template asks for NFHS-5)",
    },
    "nutrition__percentage_of_low_birth_weight_babies_born_nfhs_5_overall_district_and_s": {
        "kind": "column", "column": _col("pct_lbw", "hmis"), "unit": "%",
        "status": "proxy", "basis": "HMIS F.1 facility-reported (template asks for NFHS-5)",
    },
}

# Which survey the template names for an indicator we cannot compute. Used to
# tell the caller *what to ingest*, instead of a bare "unavailable".
MISSING_SOURCE_HINTS = (
    ("NFHS", "NFHS-5 district factsheets are not ingested."),
    ("RHS", "Rural Health Statistics is not ingested."),
    ("SRS", "Sample Registration System is not ingested."),
    ("Census", "This Census table is not in the ingested population tables."),
)

DEFAULT_MISSING_REASON = (
    "No ingested source provides this indicator. Census 2011 population tables "
    "and HMIS F.1 are the only sources currently loaded."
)


@lru_cache(maxsize=1)
def load_catalog() -> list[dict]:
    """The template's 227 indicators, each with its formula when there is one."""
    with CATALOG_PATH.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh, delimiter="\t"))

    catalog = []
    for row in rows:
        formula = FORMULAS.get(row["id"])
        entry = dict(row)
        if formula:
            entry["formula"] = formula
            entry["status"] = formula.get("status", "computed")
            entry["basis"] = formula.get("basis", "")
            entry["unit"] = formula.get("unit", "")
        else:
            entry["formula"] = None
            entry["status"] = "unavailable"
            entry["basis"] = ""
            entry["unit"] = ""
            entry["reason"] = _missing_reason(row)
        catalog.append(entry)
    return catalog


def _missing_reason(row: dict) -> str:
    haystack = f"{row['name']} {row['template_source']}"
    for token, reason in MISSING_SOURCE_HINTS:
        if token.lower() in haystack.lower():
            return reason
    return DEFAULT_MISSING_REASON
