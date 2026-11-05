import pandas as pd
from fastapi.testclient import TestClient

from app.main import app
from app.modules.kpi import aggregation

from tests.web_warehouse.helpers import new_survey
from tests.web_warehouse.test_merge import COL, PATNA, merged, put_master, web

client = TestClient(app)


def test_web_total_counts_once_per_place_year():
    df = pd.DataFrame({
        "unique_id": [PATNA, PATNA, PATNA, PATNA],
        "year": ["2019-2020", "2019-2020", "2019-2020", "2020-2021"],
        "period_hmis": ["MonthUpToJune", "MonthUpToDecember", "MonthUpToMarch", "MonthUpToMarch"],
        COL: [5, 5, 5, 6],
    })
    assert aggregation.is_web(COL) and not aggregation.is_web("deliveries_hmis")
    assert aggregation.total(df, COL) == 11  # 5 for 2019-20 once, 6 for 2020-21


def test_aggregate_endpoint_adds_web_source(db):
    s = new_survey()
    put_master("hmis_master", [
        {"survey_id": s, "unique_id": PATNA, "report_year": "2019-2020", "period": p, "deliveries": 1.0}
        for p in ("MonthUpToJune", "MonthUpToDecember", "MonthUpToMarch")])
    t = web(db, s, [["Patna", "5"], ["Gaya", "3"]])  # Gaya has no HMIS row, still counts
    merged(db, s, ["hmis"], [t])
    body = {
        "survey_id": s,
        "kpis": [{"id": "home", "aggregation": "sum", "numerator": COL}],
        "sources": [{"suffix": "_hmis", "place_key": ["unique_id"],
                     "time": ["report_year_hmis", "period_hmis"]}],
        "time": {"report_year_hmis": "2019-2020", "period_hmis": "MonthUpToMarch"},
        "trend_times": [{"report_year_hmis": "2019-2020", "period_hmis": "MonthUpToMarch"}],
    }
    resp = client.post("/kpi/aggregate", json=body)
    assert resp.status_code == 200, resp.text
    assert resp.json()["overall"]["home"]["value"] == 8  # 5 + 3, not 15 and not 5
    assert resp.json()["trend"][0]["overall"]["home"]["value"] == 8


def test_dashboard_filters_reach_website_only_rows(db):
    s = new_survey()
    put_master("hmis_master", [{"survey_id": s, "unique_id": PATNA, "report_year": "2019-2020",
                                "period": "MonthUpToMarch", "deliveries": 1.0,
                                "district_name": "Patna", "state_name": "Bihar"}])
    gaya_2021 = web(db, s, [["Gaya", "3"]], year="2020-2021")  # no HMIS row at all
    merged(db, s, ["hmis"], [gaya_2021])
    dims = {"district": {"columns": ["district_name_hmis"]}, "state": {"columns": ["state_name_hmis"]}}
    got = client.post("/kpi/dimensions", json={
        "survey_id": s, "dimensions": dims, "time_columns": ["report_year_hmis", "period_hmis"]}).json()
    assert {"report_year_hmis": "2020-2021", "period_hmis": "MonthUpToMarch"} in got["time_slices"]
    assert {"district": "Gaya", "state": "Bihar"} in got["combos"]
    resp = client.post("/kpi/aggregate", json={
        "survey_id": s, "kpis": [{"id": "home", "aggregation": "sum", "numerator": COL}],
        "sources": [{"suffix": "_hmis", "place_key": ["unique_id"], "time": ["report_year_hmis", "period_hmis"]}],
        "dimensions": dims, "filters": {"district": ["Gaya"]},
        "time": {"report_year_hmis": "2020-2021", "period_hmis": "MonthUpToMarch"}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["overall"]["home"]["value"] == 3
