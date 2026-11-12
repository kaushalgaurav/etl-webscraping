import pytest

from app.models import WebClean, WebRaw
from app.modules.web_warehouse import service

from tests.web_warehouse.helpers import new_survey, saved_table


def clean_rows(db, table):
    return db.query(WebClean).filter_by(web_table_id=table.id).all()


def test_clean_matches_districts_and_years(db):
    s = new_survey()
    t = saved_table(db, s, ["District", "Beds"], [["Patna", "10"], ["Gaya", "7"], ["Nowhere", "3"]])
    result = service.build_clean(db, survey_id=s)
    db.refresh(t)
    assert t.status == "cleaned" and result["rows"] == 3
    assert t.match_summary == {"matched": 2, "total": 3, "unmatched": ["Nowhere"],
                               "year_unrecognised": [], "duplicate_places": []}
    by_name = {r.district_name: r for r in clean_rows(db, t)}
    patna = by_name["Patna"]
    assert (patna.unique_id, patna.state_name, patna.state_id, patna.matched) == (1028, "Bihar", 10, True)
    assert (patna.year, patna.indicator, patna.value) == ("2019-2020", "beds", 10.0)
    assert by_name["Nowhere"].unique_id is None and by_name["Nowhere"].matched is False


def test_clean_parses_indian_numbers(db):
    s = new_survey()
    t = saved_table(db, s, ["District", "A", "B", "C", "D"], [["Patna", "1,23,456", "12.5%", "-", "NA"]])
    service.build_clean(db, survey_id=s)
    values = {r.indicator: r.value for r in clean_rows(db, t)}
    assert values == {"a": 123456.0, "b": 12.5, "c": None, "d": None}


def test_clean_survives_blank_and_footnote_places(db):
    s = new_survey()
    t = saved_table(db, s, ["District", "Beds"],
                    [["Patna", "1"], ["", "2"], [None, "3"], ["* Provisional", "4"], ["Total", "10"]])
    service.build_clean(db, survey_id=s)
    db.refresh(t)
    names = [r.district_name for r in clean_rows(db, t)]
    assert "Total" not in names and "Patna" in names
    assert t.match_summary["unmatched"] == ["* Provisional"] and t.match_summary["total"] == 2


def test_clean_keeps_duplicate_headers_apart(db):
    s = new_survey()
    t = saved_table(db, s, ["District", "Total", "Total."], [["Patna", "1", "2"]])
    service.build_clean(db, survey_id=s)
    assert {r.indicator: r.value for r in clean_rows(db, t)} == {"total": 1.0, "total_2": 2.0}


def test_clean_one_table_leaves_others(db):
    s = new_survey()
    a = saved_table(db, s, ["District", "Beds"], [["Patna", "1"]], name="A")
    b = saved_table(db, s, ["District", "Beds"], [["Gaya", "2"]], name="B")
    service.build_clean(db, survey_id=s)
    before = [r.id for r in clean_rows(db, b)]
    service.build_clean(db, survey_id=s, table=a.key)
    assert [r.id for r in clean_rows(db, b)] == before


def test_clean_year_column_unrecognised_reported(db):
    s = new_survey()
    t = saved_table(db, s, ["District", "Year", "Beds"], [["Patna", "2019-20", "1"], ["Gaya", "2019", "2"]],
                    year_column="Year")
    service.build_clean(db, survey_id=s)
    db.refresh(t)
    assert {r.district_name: r.year for r in clean_rows(db, t)} == {"Patna": "2019-2020", "Gaya": None}
    assert t.match_summary["year_unrecognised"] == ["2019"]


def test_clean_state_column_and_duplicates_reported(db):
    s = new_survey()
    t = saved_table(db, s, ["State", "District", "Beds"],
                    [["Bihar", "Aurangabad", "1"], ["Maharashtra", "Aurangabad", "2"], ["Bihar", "Patna", "3"],
                     ["Bihar", "Patna", "4"]], state_column="State")
    service.build_clean(db, survey_id=s)
    db.refresh(t)
    ids = {(r.state_name, r.district_name): r.unique_id for r in clean_rows(db, t)}
    assert ids[("Bihar", "Aurangabad")] == 1033 and ids[("Maharashtra", "Aurangabad")] not in (None, 1033)
    assert t.match_summary["duplicate_places"] == ["Patna"]


def test_clean_raw_ids_trace_back(db):
    s = new_survey()
    t = saved_table(db, s, ["District", "Beds", "Doctors"], [["Patna", "1", "5"], ["Gaya", "2", "6"]])
    service.build_clean(db, survey_id=s)
    for row in clean_rows(db, t):
        raw = db.get(WebRaw, row.raw_id)
        assert raw.place_text == row.district_name and float(raw.value_text) == row.value


def test_clean_is_repeatable(db):
    s = new_survey()
    t = saved_table(db, s, ["District", "Beds"], [["Patna", "1"], ["Gaya", "2"]])
    service.build_clean(db, survey_id=s)
    service.build_clean(db, survey_id=s)
    assert len(clean_rows(db, t)) == 2


def test_clean_errors(db):
    with pytest.raises(ValueError, match="No Website tables found for survey 999999"):
        service.build_clean(db, survey_id=999999)
    s = new_survey()
    saved_table(db, s, ["District", "Beds"], [["Patna", "1"]])
    with pytest.raises(ValueError, match="Unknown cleaning operation"):
        service.build_clean(db, operations=["polish"], survey_id=s)


def test_indicator_names_never_collide():
    assert list(service._indicators(["Total %", "Total", "Total 2", "Total_3", "Total."]).values()) == [
        "total", "total_2", "total_2_2", "total_3", "total_4"]


LONG = ["District", "Indicator", "NFHS-5"]


def test_clean_long_table_one_row_per_district(db):
    s = new_survey()
    t = saved_table(db, s, LONG, [["Patna", "Women literate (%)", "70.1"],
                                  ["Patna", "Children stunted (%)", "30.5"],
                                  ["Gaya", "Women literate (%)", "55.0"]],
                    indicator_column="Indicator")
    service.build_clean(db, survey_id=s)
    db.refresh(t)
    # Districts repeat once per indicator; that is the table's shape, not duplicates.
    assert t.match_summary["duplicate_places"] == [] and t.match_summary["total"] == 2
    # A district without an indicator gets no row for it (Gaya has no stunting value).
    values = {(r.district_name, r.indicator): r.value for r in clean_rows(db, t)}
    assert len(clean_rows(db, t)) == 3 and values == {("Patna", "women_literate"): 70.1, ("Patna", "children_stunted"): 30.5,
                      ("Gaya", "women_literate"): 55.0}


def test_long_table_repeated_indicator_is_reported(db):
    s = new_survey()
    t = saved_table(db, s, LONG, [["Patna", "Women literate (%)", "70.1"],
                                  ["Patna", "Women literate (%)", "50.0"],
                                  ["Gaya", "Women literate (%)", "55.0"]],
                    indicator_column="Indicator")
    service.build_clean(db, survey_id=s)
    db.refresh(t)
    # Two values for one district + indicator (e.g. Total and Rural rows): only one
    # can be kept, so the place is flagged rather than silently overwritten.
    assert "Patna" in t.match_summary["duplicate_places"]
    assert "Gaya" not in t.match_summary["duplicate_places"]
