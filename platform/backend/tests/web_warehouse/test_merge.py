import pandas as pd
import pytest

from app.db.session import engine
from app.models import Dataset
from app.modules.merging import run, survey, web_join
from app.modules.web_warehouse import schema as web_schema
from app.modules.web_warehouse import service

from tests.web_warehouse.helpers import new_survey, saved_table

PATNA, GAYA = 1028, 1034


def put_master(table: str, rows: list[dict]) -> None:
    """Append rows to a stand-in master table, adding any column it lacks yet."""
    from sqlalchemy import inspect, text
    frame = pd.DataFrame(rows)
    with engine.begin() as conn:
        if inspect(conn).has_table(table):
            have = {c["name"] for c in inspect(conn).get_columns(table)}
            for column in frame.columns:
                if column not in have:
                    conn.execute(text(f'ALTER TABLE {table} ADD COLUMN "{column}" TEXT'))
    frame.to_sql(table, engine, if_exists="append", index=False)


def census(s):
    put_master("census_master", [
        {"survey_id": s, "level": "district", "unique_id": PATNA, "total_population": 100.0},
        {"survey_id": s, "level": "district", "unique_id": GAYA, "total_population": 50.0}])


def hmis(s):
    put_master("hmis_master", [
        {"survey_id": s, "unique_id": PATNA, "report_year": "2018-2019", "period": "MonthUpToMarch", "deliveries": 8.0},
        {"survey_id": s, "unique_id": PATNA, "report_year": "2019-2020", "period": "MonthUpToMarch", "deliveries": 9.0}])


def web(db, s, rows, name="Deliveries", year="2019-2020"):
    t = saved_table(db, s, ["District", "Home deliveries"], rows, name=name, year=year)
    service.build_clean(db, survey_id=s, table=t.key)
    service.build_master(db, survey_id=s, table=t.key)
    return t


def merged(db, s, connectors, tables):
    run.run_merge(db, {"survey_id": s, "connectors": connectors, "web_tables": [t.key for t in tables]})
    return survey.load_dashboard(db, s)


COL = "web_deliveries_home_deliveries"


def test_web_joins_hmis_on_district_and_year(db):
    s = new_survey()
    hmis(s)
    t = web(db, s, [["Patna", "5"]])
    df = merged(db, s, ["hmis"], [t])
    row = df[(df.unique_id == PATNA) & (df.year == "2019-2020")]
    assert row[COL].item() == 5 and row["deliveries_hmis"].item() == 9
    assert df[(df.unique_id == PATNA) & (df.year == "2018-2019")][COL].isna().all()


def test_census_attaches_by_district_only(db):
    s = new_survey()
    census(s)
    hmis(s)
    t = web(db, s, [["Patna", "5"]])
    df = merged(db, s, ["census", "hmis"], [t])
    patna = df[df.unique_id == PATNA]
    assert sorted(patna["year"]) == ["2018-2019", "2019-2020"]
    assert set(patna["total_population_census"]) == {100.0}


def test_web_row_without_hmis_partner_kept(db):
    s = new_survey()
    hmis(s)
    t = web(db, s, [["Gaya", "4"]], year="2020-2021")
    df = merged(db, s, ["hmis"], [t])
    gaya = df[df.unique_id == GAYA]
    assert gaya["year"].tolist() == ["2020-2021"] and gaya[COL].tolist() == [4]
    assert gaya["web_district_name"].tolist() == ["Gaya"]


def test_same_name_two_years_share_columns(db):
    s = new_survey()
    census(s)
    a = web(db, s, [["Patna", "5"]], year="2018-2019")
    b = web(db, s, [["Patna", "6"]], year="2019-2020")
    df = merged(db, s, ["census"], [a, b])
    assert [c for c in df.columns if c.startswith("web_deliveries")] == [COL]
    assert dict(zip(df[df.unique_id == PATNA]["year"], df[df.unique_id == PATNA][COL])) == {
        "2018-2019": 5, "2019-2020": 6}


def test_same_name_overlap_refused(db):
    s = new_survey()
    census(s)
    a = web(db, s, [["Patna", "5"]])
    b = saved_table(db, s, ["District", "Home deliveries"], [["Patna", "7"]], name="Deliveries",
                    page="https://other.example.gov.in/")
    service.build_clean(db, survey_id=s, table=b.key)
    service.build_master(db, survey_id=s, table=b.key)
    with pytest.raises(run.MergeRequestError) as e:
        merged(db, s, ["census"], [a, b])
    assert str(e.value) == ("Two ticked Website tables are both called 'Deliveries' and both have "
                            "'home_deliveries' for 2019-2020; rename one.")


def test_web_table_errors(db):
    s = new_survey()
    census(s)
    other = web(db, new_survey(), [["Patna", "5"]])
    with pytest.raises(run.MergeRequestError, match="is not part of survey"):
        merged(db, s, ["census"], [other])
    raw_only = saved_table(db, s, ["District", "Home deliveries"], [["Patna", "1"]], name="Raw")
    with pytest.raises(run.MergeRequestError, match="has no master rows; rebuild its master first"):
        merged(db, s, ["census"], [raw_only])
    lone = web(db, s, [["Patna", "5"]], name="Lonely")
    with pytest.raises(run.MergeRequestError, match="at least two"):
        run.run_merge(db, {"survey_id": s, "connectors": [], "web_tables": [lone.key]})


def test_web_column_long_and_unicode_names():
    long_name = "जिला स्वास्थ्य " + "District health statistics for every block " * 2
    a, b = web_join.web_column(long_name, "beds"), web_join.web_column(long_name, "doctors")
    assert len(a) <= 63 and len(b) <= 63 and a != b and a.startswith("web_")
    hindi = web_join.web_column("जिला स्वास्थ्य", "beds")
    assert hindi.startswith("web_t") and hindi != web_join.web_column("राज्य", "beds")


def test_column_budget_refuses(db, monkeypatch):
    s = new_survey()
    census(s)
    t = web(db, s, [["Patna", "5"]], name=f"Budget table {s}")  # adds a new column
    monkeypatch.setattr(web_schema, "MAX_DASHBOARD_COLUMNS", 5)
    with pytest.raises(run.MergeRequestError) as e:
        merged(db, s, ["census"], [t])
    assert "the limit is 5" in str(e.value) and "Untick some Website tables" in str(e.value)


def test_merge_without_web_tables_unchanged(db):
    s = new_survey()
    census(s)
    hmis(s)
    run.run_merge(db, {"survey_id": s, "connectors": ["census", "hmis"]})
    df = survey.load_dashboard(db, s)
    assert "deliveries_hmis" in df.columns and "total_population_census" in df.columns
    assert df[[c for c in df.columns if c.startswith("web_")]].isna().all().all()


def test_merge_job_end_to_end(db):
    s = new_survey()
    census(s)
    t = web(db, s, [["Patna", "5"], ["Gaya", "3"]])
    dataset, summary = run.run_merge(db, {"survey_id": s, "connectors": ["census"], "web_tables": [t.key]})
    assert db.get(Dataset, dataset.id).stage == "master" and summary["table"] == "dashboard_master"
    df = survey.load_dashboard(db, s)
    assert sorted(df[COL].dropna()) == [3, 5] and set(df["survey_id"]) == {s}


def test_refused_merge_keeps_survey_rows(db, monkeypatch):
    s = new_survey()
    census(s)
    first = web(db, s, [["Patna", "5"]])
    merged(db, s, ["census"], [first])
    before = len(survey.load_dashboard(db, s))
    other = web(db, s, [["Patna", "9"]], name="Brand new table name")
    from sqlalchemy import inspect
    existing = len(inspect(db.connection()).get_columns("dashboard_master"))
    monkeypatch.setattr(web_schema, "MAX_DASHBOARD_COLUMNS", existing)
    with pytest.raises(run.MergeRequestError):
        merged(db, s, ["census"], [first, other])
    assert len(survey.load_dashboard(db, s)) == before


def test_budget_allows_merge_adding_no_columns(db, monkeypatch):
    s = new_survey()
    census(s)
    t = web(db, s, [["Patna", "5"]])
    merged(db, s, ["census"], [t])
    monkeypatch.setattr(web_schema, "MAX_DASHBOARD_COLUMNS", 3)  # table is already far past it
    assert len(merged(db, s, ["census"], [t])) >= 1


def test_two_names_filling_one_column_refused(db):
    s = new_survey()
    census(s)
    a = saved_table(db, s, ["District", "c"], [["Patna", "1"]], name="a b")
    b = saved_table(db, s, ["District", "b c"], [["Patna", "2"]], name="a")
    for t in (a, b):
        service.build_clean(db, survey_id=s, table=t.key)
        service.build_master(db, survey_id=s, table=t.key)
    with pytest.raises(run.MergeRequestError) as e:
        merged(db, s, ["census"], [a, b])
    assert str(e.value) == ("Website tables 'a b' and 'a' would both fill column 'web_a_b_c'; "
                            "rename one.")
