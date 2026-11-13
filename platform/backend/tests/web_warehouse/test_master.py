from app.crud import dataset as dataset_crud
from app.models import Dataset, WebMaster, WebTable
from app.modules.scraping import importer
from app.modules.scraping import service as scrape_service
from app.modules.web_warehouse import service

from tests.scraping import helpers as scrape_helpers
from tests.web_warehouse.helpers import new_survey, saved_table


def master_rows(db, table):
    return db.query(WebMaster).filter_by(web_table_id=table.id).all()


def test_master_last_row_wins_and_skips_unmatched(db):
    s = new_survey()
    t = saved_table(db, s, ["District", "Beds"], [["Patna", "1"], ["Nowhere", "2"], ["Patna", "3"], ["Gaya", "-"]])
    service.build_clean(db, survey_id=s)
    result = service.build_master(db, survey_id=s)
    rows = master_rows(db, t)
    assert [(r.district_name, r.year, r.indicator, r.value) for r in rows] == [("Patna", "2019-2020", "beds", 3.0)]
    assert rows[0].unique_id == 1028 and result["rows"] == 1 and result["skipped"] == []


def test_master_skips_uncleaned(db):
    s = new_survey()
    saved_table(db, s, ["District", "Beds"], [["Patna", "1"]], name="Raw only")
    result = service.build_master(db, survey_id=s)
    assert result["skipped"] == ["Raw only"] and result["rows"] == 0


def test_master_rebuild_replaces(db):
    s = new_survey()
    t = saved_table(db, s, ["District", "Beds"], [["Patna", "1"]])
    service.build_clean(db, survey_id=s)
    service.build_master(db, survey_id=s)
    service.build_master(db, survey_id=s)
    assert len(master_rows(db, t)) == 1


def test_list_tables_reasons(db):
    s = new_survey()
    good = saved_table(db, s, ["District", "Beds"], [["Patna", "1"]], name="Good")
    saved_table(db, s, ["District", "Beds"], [["Patna", "1"]], name="Not cleaned")
    saved_table(db, s, ["Menu", "Links"], [["Home", "About"]], name="No place", place=None)
    saved_table(db, s, ["District", "Beds"], [["Nowhere", "1"]], name="No match")
    saved_table(db, s, ["District", "Year", "Beds"], [["Patna", "2019", "1"]], name="No year", year_column="Year")
    for name in ("Good", "No place", "No match", "No year"):
        t = db.query(WebTable).filter_by(survey_id=s, name=name).one()
        service.build_clean(db, survey_id=s, table=t.key)
    listed = {t["name"]: t for t in service.list_tables(db, s)}
    assert {n: t["reason"] for n, t in listed.items()} == {
        "Good": None, "Not cleaned": "not cleaned", "No place": "no district column",
        "No match": "no district matched", "No year": "no year recognised"}
    g = listed["Good"]
    assert g["mergeable"] is True and g["web_table_id"] == good.key and g["rows"] == 1 and g["values"] == 1
    assert g["status"] == "cleaned" and g["match_summary"]["matched"] == 1
    assert listed["Not cleaned"]["mergeable"] is False


def test_resave_resets_mergeable(db, httpserver, loopback_ok):
    s = new_survey()
    for round_ in (1, 2):
        scan = scrape_helpers.run(db, scrape_helpers.standard_site(httpserver), loopback_ok)
        csv = [f for f in scrape_service.manifest(db, scan.key)["tables"] if f["kind"] == "file_table"][0]
        items = [{"finding_id": csv["finding_id"], "state_name": "Bihar", "year": "2019-2020"}]
        importer.claim_for_import(db, scan.key, items)
        importer.run_import(db, scan.key, s, items)
        if round_ == 1:
            service.build_clean(db, survey_id=s)
            assert service.list_tables(db, s)[0]["status"] == "cleaned"
    [listed] = service.list_tables(db, s)
    assert listed["status"] == "saved" and listed["mergeable"] is False and listed["reason"] == "not cleaned"


def test_dataset_record_card_reads_its_raw_rows(db):
    s = new_survey()
    a = saved_table(db, s, ["District", "Beds"], [["Patna", "1"], ["Gaya", "2"]], name="A")
    saved_table(db, s, ["District", "Beds"], [["Patna", "9"]], name="B")
    card = dataset_crud.create_dataset_shell(db, name="A", stage="raw", connector="web_table",
                                             source_ref=None, table_name="web_raw", survey_id=s)
    a.dataset_id = card.id
    db.commit()
    total, page = dataset_crud.load_page(db.get(Dataset, card.id), 50, 0)
    assert total == 2 and sorted(page["value_text"]) == ["1", "2"]


def test_summary_and_export(db, tmp_path):
    s = new_survey()
    saved_table(db, s, ["District", "Beds"], [["Patna", "1"]])
    service.build_clean(db, survey_id=s)
    service.build_master(db, survey_id=s)
    summary = service.summary(db, survey_id=s)
    assert summary["tables"] == 1 and summary["web_raw"]["rows"] == 1 and summary["web_master"]["rows"] == 1
    out = service.export_layer(db, "web_master", out_dir=tmp_path, survey_id=s)
    assert out["rows"] == 1 and out["path"].endswith(f"web_master__survey_{s}.csv")
    assert open(out["path"], "rb").read().startswith(b"\xef\xbb\xbf")


def test_master_keeps_every_indicator_of_a_long_table(db):
    s = new_survey()
    t = saved_table(db, s, ["District", "Indicator", "NFHS-5"],
                    [["Patna", "Women literate (%)", "70.1"], ["Patna", "Children stunted (%)", "30.5"],
                     ["Gaya", "Women literate (%)", "55.0"]], indicator_column="Indicator")
    service.build_clean(db, survey_id=s)
    service.build_master(db, survey_id=s)
    rows = db.query(WebMaster).filter_by(web_table_id=t.id).all()
    assert sorted((r.district_name, r.indicator, r.value) for r in rows) == [
        ("Gaya", "women_literate", 55.0), ("Patna", "children_stunted", 30.5),
        ("Patna", "women_literate", 70.1)]
