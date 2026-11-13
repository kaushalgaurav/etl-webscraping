import json

import pytest

from app.models import ScrapeFinding
from app.modules.scraping import service

from tests.scraping import helpers


def done_scan(db, httpserver, loopback_ok):
    return helpers.run(db, helpers.standard_site(httpserver), loopback_ok)


def finding(db, scan, **filters):
    return db.query(ScrapeFinding).filter_by(scan_id=scan.id, **filters).order_by(ScrapeFinding.position).first()


def test_manifest_groups_and_order(db, httpserver, loopback_ok):
    scan = done_scan(db, httpserver, loopback_ok)
    m = service.manifest(db, scan.key)
    assert m["scan_id"] == scan.key and m["status"] == "done" and m["summary"] == scan.summary
    assert len(m["tables"]) == 3 and len(m["files"]) == 1 and len(m["text"]) == 1 and m["blocked"] == []
    titles = [t["title"] for t in m["tables"]]
    assert titles.index("District beds") < titles.index("data.csv")
    table = m["tables"][titles.index("data.csv")]
    assert set(table) >= {"finding_id", "kind", "title", "locator", "rows", "columns", "column_names",
                          "preview", "signals", "suggested", "default_name", "imported_dataset_id"}
    assert table["default_name"] == "localhost - data.csv"
    f = m["files"][0]
    assert f["name"] == "data.csv" and f["produced"] == [table["finding_id"]]
    assert set(f) >= {"finding_id", "url", "content_type", "bytes", "extracted"}
    t = m["text"][0]
    assert t["source"] == "page" and t["characters"] > 100 and len(t["snippet"]) <= 1000
    json.dumps(m, default=str, allow_nan=False)  # JSON-safe


def test_manifest_not_found(db):
    with pytest.raises(service.ScanNotFound):
        service.manifest(db, "nope")


def test_table_rows_paging(db, httpserver, loopback_ok):
    scan = done_scan(db, httpserver, loopback_ok)
    table = finding(db, scan, kind="file_table")
    page = service.table_rows(db, scan.key, table.key, limit=10, offset=30)
    assert page["total"] == 38 and len(page["rows"]) == 8
    assert page["columns"] == ["District", "Beds"] and page["rows"][0] == {"District": "District 30", "Beds": "300"}


def test_table_rows_rejects(db, httpserver, loopback_ok):
    scan = done_scan(db, httpserver, loopback_ok)
    with pytest.raises(service.ScanRequestError):
        service.table_rows(db, scan.key, finding(db, scan, kind="text").key, 10, 0)
    with pytest.raises(service.ScanNotFound):
        service.table_rows(db, scan.key, "nope", 10, 0)


def test_download_table_file_text(db, httpserver, loopback_ok):
    scan = done_scan(db, httpserver, loopback_ok)
    body, name, media = service.download(db, scan.key, finding(db, scan, kind="file_table").key)
    assert body.startswith("﻿".encode("utf-8")) and media == "text/csv; charset=utf-8"
    assert name == "localhost - data.csv.csv"
    body, name, media = service.download(db, scan.key, finding(db, scan, kind="file").key)
    assert body == helpers.district_csv() and name == "data.csv" and media.startswith("text/csv")
    body, name, media = service.download(db, scan.key, finding(db, scan, kind="text").key)
    assert media == "text/plain; charset=utf-8" and name.endswith(".txt") and b"bed counts" in body


def test_reads_after_save_are_conflicts(db, httpserver, loopback_ok):
    scan = done_scan(db, httpserver, loopback_ok)
    table = finding(db, scan, kind="file_table")
    scan.status = "imported"
    db.commit()
    for call in (lambda: service.table_rows(db, scan.key, table.key, 10, 0),
                 lambda: service.download(db, scan.key, table.key)):
        with pytest.raises(service.ScanConflict) as e:
            call()
        assert str(e.value) == "This search was saved; open the saved dataset instead."


def test_delete_scan(db, httpserver, loopback_ok):
    scan = done_scan(db, httpserver, loopback_ok)
    from app.modules.scraping import store

    service.delete_scan(db, scan.key)
    assert scan.key not in [s["scan_id"] for s in service.list_scans(db, 100)]
    assert not store.scan_dir(scan.key).exists()
    with pytest.raises(service.ScanNotFound):
        service.manifest(db, scan.key)


@pytest.mark.parametrize("status", ["running", "importing", "imported"])
def test_delete_scan_refused(db, httpserver, loopback_ok, status):
    scan = done_scan(db, httpserver, loopback_ok)
    scan.status = status
    db.commit()
    with pytest.raises(service.ScanConflict):
        service.delete_scan(db, scan.key)


def test_list_scans_newest_first(db, httpserver, loopback_ok):
    first = done_scan(db, httpserver, loopback_ok)
    second = done_scan(db, httpserver, loopback_ok)
    listed = [s["scan_id"] for s in service.list_scans(db, 100)]
    assert listed.index(second.key) < listed.index(first.key)
    assert set(service.list_scans(db, 1)[0]) == {"scan_id", "url", "status", "created_at",
                                                  "expires_at", "summary"}


def test_manifest_ids_are_uuids(db, httpserver, loopback_ok):
    import uuid
    scan = helpers.run(db, helpers.standard_site(httpserver), loopback_ok)
    m = service.manifest(db, scan.key)
    assert uuid.UUID(m["scan_id"]) == scan.external_id
    assert all(uuid.UUID(t["finding_id"]) for t in m["tables"])
    produced = [p for f in m["files"] for p in f["produced"]]
    assert produced and all(uuid.UUID(p) for p in produced)
    children = [t for t in m["tables"] if t["parent_id"]]
    assert children and all(t["parent_id"] in {f["finding_id"] for f in m["files"]} for t in children)
