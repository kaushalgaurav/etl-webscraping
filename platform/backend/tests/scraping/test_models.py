from app.models import ScrapeFinding, ScrapeScan


def test_scan_with_finding_round_trips(db):
    scan = ScrapeScan(url="https://example.gov.in/x", status="running")
    db.add(scan)
    db.flush()
    db.add(ScrapeFinding(scan_id=scan.id, position=0, kind="html_table", title="T",
                         locator={"source": "page", "table_index": 0},
                         signals={"numeric_share": 0.5}))
    db.commit()
    got = db.query(ScrapeFinding).filter_by(scan_id=scan.id).one()
    assert got.locator["table_index"] == 0 and len(got.id) == 12
    assert scan.blocked == [] and scan.options == {}
