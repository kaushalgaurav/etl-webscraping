from fastapi.testclient import TestClient

from app.main import app
from app.modules.web_warehouse.years import financial_years

from tests.web_warehouse.helpers import new_survey, saved_table

client = TestClient(app)


def test_router_web_clean_and_master(db):
    s = new_survey()
    t = saved_table(db, s, ["District", "Beds"], [["Patna", "1"]])
    resp = client.post(f"/warehouse/web/clean?survey_id={s}&table={t.key}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["tables"][0]["web_table_id"] == t.key
    resp = client.post(f"/warehouse/web/master?survey_id={s}")
    assert resp.status_code == 200 and resp.json()["rows"] == 1
    rows = client.get(f"/warehouse/web/rows?layer=web_master&survey_id={s}")
    assert rows.status_code == 200, rows.text
    assert rows.json()["rows"][0]["district_name"] == "Patna"
    assert client.get(f"/warehouse/web?survey_id={s}").json()["tables"] == 1


def test_router_table_param_rejected_for_census():
    resp = client.post("/warehouse/census/clean?survey_id=1&table=abc")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "'table' applies only to the web connector."


def test_router_web_tables_and_years(db):
    s = new_survey()
    saved_table(db, s, ["District", "Beds"], [["Patna", "1"]], name="Listed")
    listed = client.get(f"/warehouse/web/tables?survey_id={s}")
    assert listed.status_code == 200 and [t["name"] for t in listed.json()] == ["Listed"]
    assert client.get("/warehouse/web/tables").status_code == 422
    years = client.get("/warehouse/web/years").json()
    assert years[0] == "2005-2006" and years[-1] == financial_years()[-1]
    assert client.post("/warehouse/web/clean?survey_id=99999999").status_code == 400
