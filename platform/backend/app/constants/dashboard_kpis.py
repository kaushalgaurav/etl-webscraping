"""The 28 dashboard KPIs — the fixed card set behind the Public Sector Dashboard.

Distinct from the other two KPI surfaces:

* `kpi_catalog` holds the report template's 227 indicators (the deliverable).
* `kpi.dynamic` asks Claude to propose KPIs for an arbitrary merged dataset.
* This module is the agreed dashboard set: fixed ids KPI-01..KPI-28, fixed
  formulas, fixed domains. Nothing here is generated, so a card's number is
  the same on every run and traceable to its columns.

AGGREGATION kinds, following the specification sheet:
  sum           - add the column across the selected rows.
  literacy_pct  - sum(numerator)/(sum(denominator) - sum(under-7s))*100. The
                  official Census literacy rate is literates over the
                  population aged 7 and above: under-7s are counted illiterate
                  by definition, so including them understates every district
                  by several points (63.17% against India's true 72.99%).
  weighted_pct  - sum(numerator)/sum(denominator)*100. The correct way to roll
                  a rate up across districts: averaging per-district rates
                  weights a village the same as a city.
  official_pct  - HMIS publishes its own percentage; this is that column,
                  averaged across districts. Flagged, because without the
                  underlying counts it cannot be population-weighted.
  per_1000 / per_100000 - sum(numerator)/sum(denominator)*K.
  difference    - one KPI's value minus another's.
"""

C = "_census"
H = "_hmis"

KPIS: list[dict] = [
    {"id": "KPI-01", "name": "Total Population", "domain": "Demography",
     "aggregation": "sum", "column": f"total_population_both_sexes{C}",
     "unit": "persons", "meaning": "Total population across districts",
     "visualisation": "kpi_card"},

    {"id": "KPI-02", "name": "Female Population Share", "domain": "Gender",
     "aggregation": "weighted_pct", "numerator": f"total_population_females{C}",
     "denominator": f"total_population_both_sexes{C}", "unit": "%",
     "meaning": "Female population as % of total population", "visualisation": "donut_chart"},

    {"id": "KPI-03", "name": "Literacy Rate", "domain": "Education",
     "aggregation": "literacy_pct", "numerator": f"literate_population_both_sexes{C}",
     "denominator": f"total_population_both_sexes{C}", "denominator_subtrahend": f"persons_aged_0_to_6{C}", "unit": "%",
     "meaning": "Overall literacy level", "visualisation": "bar_chart"},

    {"id": "KPI-04", "name": "Female Literacy Rate", "domain": "Education",
     "aggregation": "literacy_pct", "numerator": f"literate_population_females{C}",
     "denominator": f"total_population_females{C}", "denominator_subtrahend": f"females_aged_0_to_6{C}", "unit": "%",
     "meaning": "Female literacy level", "visualisation": "bar_chart"},

    {"id": "KPI-05", "name": "Male Literacy Rate", "domain": "Education",
     "aggregation": "literacy_pct", "numerator": f"literate_population_males{C}",
     "denominator": f"total_population_males{C}", "denominator_subtrahend": f"males_aged_0_to_6{C}", "unit": "%",
     "meaning": "Male literacy level", "visualisation": "bar_chart"},

    {"id": "KPI-06", "name": "Gender Literacy Gap", "domain": "Education",
     "aggregation": "difference", "minuend": "KPI-05", "subtrahend": "KPI-04",
     "unit": "pp", "meaning": "Literacy inequality by sex", "visualisation": "kpi_card"},

    {"id": "KPI-07", "name": "Work Participation Rate", "domain": "Employment",
     "aggregation": "weighted_pct", "numerator": f"total_worker_population_both_sexes{C}",
     "denominator": f"total_population_both_sexes{C}", "unit": "%",
     "meaning": "Share of population classified as workers", "visualisation": "bar_chart"},

    {"id": "KPI-08", "name": "Female Work Participation Rate", "domain": "Employment",
     "aggregation": "weighted_pct", "numerator": f"total_worker_population_females{C}",
     "denominator": f"total_population_females{C}", "unit": "%",
     "meaning": "Female workforce participation", "visualisation": "bar_chart"},

    {"id": "KPI-09", "name": "Households", "domain": "Housing",
     "aggregation": "sum", "column": f"number_of_households{C}",
     "unit": "households", "meaning": "Total households", "visualisation": "kpi_card"},

    {"id": "KPI-10", "name": "Institutional Delivery Rate", "domain": "Maternal Health",
     "aggregation": "official_pct", "column": f"pct_inst_del{H}", "unit": "%",
     "meaning": "Share of deliveries occurring in institutions", "visualisation": "kpi_card"},

    {"id": "KPI-11", "name": "Safe Delivery Rate", "domain": "Maternal Health",
     "aggregation": "official_pct", "column": f"pct_safe_del{H}", "unit": "%",
     "meaning": "Share of deliveries classified as safe", "visualisation": "kpi_card"},

    {"id": "KPI-12", "name": "ANC 4+ Coverage", "domain": "Maternal Health",
     "aggregation": "official_pct", "column": f"pct_anc_4plus{H}", "unit": "%",
     "meaning": "Women receiving 4+ ANC visits", "visualisation": "bar_chart"},

    {"id": "KPI-13", "name": "ANC in 1st Trimester", "domain": "Maternal Health",
     "aggregation": "official_pct", "column": f"pct_anc_1st_tri{H}", "unit": "%",
     "meaning": "Early ANC registration", "visualisation": "bar_chart"},

    {"id": "KPI-14", "name": "Postnatal Care Coverage", "domain": "Maternal Health",
     "aggregation": "official_pct", "column": f"pct_pnc_48h_14d{H}", "unit": "%",
     "meaning": "PNC coverage within defined period", "visualisation": "bar_chart"},

    {"id": "KPI-15", "name": "C-Section Rate", "domain": "Maternal Health",
     "aggregation": "official_pct", "column": f"pct_csec{H}", "unit": "%",
     "meaning": "Share of deliveries by C-section", "visualisation": "kpi_card"},

    {"id": "KPI-16", "name": "Breastfeeding Within 1 Hour", "domain": "Child Health",
     "aggregation": "official_pct", "column": f"pct_bf_1hr{H}", "unit": "%",
     "meaning": "Early initiation of breastfeeding", "visualisation": "bar_chart"},

    {"id": "KPI-17", "name": "BCG Coverage", "domain": "Immunization",
     "aggregation": "official_pct", "column": f"pct_bcg{H}", "unit": "%",
     "meaning": "BCG vaccination coverage", "visualisation": "bar_chart"},

    {"id": "KPI-18", "name": "OPV-0 Coverage", "domain": "Immunization",
     "aggregation": "official_pct", "column": f"pct_opv0{H}", "unit": "%",
     "meaning": "Birth-dose OPV coverage", "visualisation": "bar_chart"},

    {"id": "KPI-19", "name": "Measles Coverage", "domain": "Immunization",
     "aggregation": "official_pct", "column": f"pct_measles_mr{H}", "unit": "%",
     "meaning": "Measles/MR coverage", "visualisation": "bar_chart"},

    {"id": "KPI-20", "name": "Vitamin A Coverage", "domain": "Child Health",
     "aggregation": "official_pct", "column": f"pct_vita1{H}", "unit": "%",
     "meaning": "Vitamin A dose coverage", "visualisation": "bar_chart"},

    {"id": "KPI-21", "name": "Infant Mortality Rate", "domain": "Mortality",
     "aggregation": "per_1000", "numerator": f"infant_deaths{H}",
     "denominator": f"live_births{H}", "unit": "per 1,000 live births",
     "meaning": "Infant deaths per 1,000 live births", "visualisation": "map"},

    {"id": "KPI-22", "name": "Maternal Mortality Proxy", "domain": "Mortality",
     "aggregation": "per_100000", "numerator": f"maternal_deaths{H}",
     "denominator": f"live_births{H}", "unit": "per 100,000 live births",
     "meaning": "Maternal deaths per 100,000 live births; use as dataset proxy",
     "visualisation": "map"},

    {"id": "KPI-23", "name": "OPD Utilization", "domain": "Health Services",
     "aggregation": "per_1000", "numerator": f"opd_total{H}",
     "denominator": f"total_population_both_sexes{C}", "unit": "per 1,000",
     "meaning": "Outpatient service utilization", "visualisation": "bar_chart"},

    {"id": "KPI-24", "name": "IPD Utilization", "domain": "Health Services",
     "aggregation": "per_1000", "numerator": f"ipd{H}",
     "denominator": f"total_population_both_sexes{C}", "unit": "per 1,000",
     "meaning": "Inpatient service utilization", "visualisation": "bar_chart"},

    {"id": "KPI-25", "name": "Live Births", "domain": "Maternal & Child Health",
     "aggregation": "sum", "column": f"live_births{H}", "unit": "births",
     "meaning": "Recorded live births", "visualisation": "kpi_card"},

    {"id": "KPI-26", "name": "Total Deliveries", "domain": "Maternal Health",
     "aggregation": "sum", "column": f"total_del{H}", "unit": "deliveries",
     "meaning": "Recorded deliveries", "visualisation": "kpi_card"},

    {"id": "KPI-27", "name": "Infant Deaths", "domain": "Mortality",
     "aggregation": "sum", "column": f"infant_deaths{H}", "unit": "deaths",
     "meaning": "Recorded infant deaths", "visualisation": "kpi_card"},

    {"id": "KPI-28", "name": "Maternal Deaths", "domain": "Mortality",
     "aggregation": "sum", "column": f"maternal_deaths{H}", "unit": "deaths",
     "meaning": "Recorded maternal deaths", "visualisation": "kpi_card"},
]

BY_ID = {k["id"]: k for k in KPIS}


def source_formula(kpi: dict) -> str:
    """The Source / Formula cell of the specification sheet."""
    agg = kpi["aggregation"]
    if agg == "sum" or agg == "official_pct":
        return kpi["column"]
    if agg == "weighted_pct":
        return f"{kpi['numerator']} / {kpi['denominator']}"
    if agg == "literacy_pct":
        return (f"{kpi['numerator']} / "
                f"({kpi['denominator']} - {kpi['denominator_subtrahend']})")
    if agg == "per_1000":
        return f"{kpi['numerator']} / {kpi['denominator']} * 1000"
    if agg == "per_100000":
        return f"{kpi['numerator']} / {kpi['denominator']} * 100000"
    if agg == "difference":
        return f"{BY_ID[kpi['minuend']]['name']} - {BY_ID[kpi['subtrahend']]['name']}"
    return ""


def required_columns(kpi: dict) -> list[str]:
    return [c for c in (kpi.get("column"), kpi.get("numerator"),
                        kpi.get("denominator"), kpi.get("denominator_subtrahend")) if c]
