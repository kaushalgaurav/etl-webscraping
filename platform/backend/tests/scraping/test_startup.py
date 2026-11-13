import tempfile
from pathlib import Path

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.modules.scraping import service


def test_startup_cleanup_survives_missing_scrape_tables():
    """Restarting before `alembic upgrade head` must not take the app down."""
    path = Path(tempfile.mkdtemp()) / "no-scrape.db"
    engine = create_engine(f"sqlite:///{path}")
    tables = [t for name, t in Base.metadata.tables.items() if not name.startswith("scrape_")]
    Base.metadata.create_all(engine, tables=tables)
    assert "scrape_scans" not in inspect(engine).get_table_names()
    db = sessionmaker(bind=engine)()
    try:
        assert service.startup_cleanup(db) is False
        db.execute(Base.metadata.tables["jobs"].select())  # session still usable
    finally:
        db.close()


def test_startup_cleanup_runs_when_tables_exist(db):
    assert service.startup_cleanup(db) is True


def test_startup_sweeps_orphan_folders(db, httpserver, loopback_ok):
    from app.modules.scraping import store
    from tests.scraping import helpers
    scan = helpers.run(db, helpers.standard_site(httpserver), loopback_ok)
    orphan = store.scan_dir("old-id-123")
    orphan.mkdir(parents=True)
    (orphan / "x.txt").write_text("left over")
    assert service.startup_cleanup(db) is True
    assert not orphan.exists() and store.scan_dir(scan.key).exists()
