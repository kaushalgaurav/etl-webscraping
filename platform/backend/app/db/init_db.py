"""Schema migration and first-boot seeding."""
from pathlib import Path

from alembic import command
from alembic.config import Config

from app.core.config import BACKEND_DIR
from app.db.session import SessionLocal

ALEMBIC_INI = BACKEND_DIR / "alembic.ini"


def _alembic_config() -> Config:
    config = Config(str(ALEMBIC_INI))
    # Paths in alembic.ini are relative to the backend directory, which is not
    # necessarily the working directory the app was started from.
    config.set_main_option("script_location", str(Path(BACKEND_DIR) / "alembic"))
    return config


def run_migrations() -> None:
    """Bring the schema to head.

    Migrations rather than `Base.metadata.create_all()`: create_all builds the
    tables but writes no alembic_version row, so a fresh database would then
    reject `alembic upgrade head` with "table already exists" — the schema
    would exist while Alembic believed nothing had run. Running the migrations
    here means one path to a schema, whether the database is new or old.

    Only the ORM tables are covered. The warehouse layers (census_raw,
    hmis_master, dashboard_master, ds_*) are shaped by whatever the source
    supplies and are created at ingest time.
    """
    command.upgrade(_alembic_config(), "head")


def init_db() -> None:
    #run_migrations()

    # Load the version-controlled source registry (no-op once populated).
    from app.modules.jobs.service import reap_orphaned_jobs
    from app.modules.sources.service import seed_sources

    db = SessionLocal()
    try:
        seed_sources(db)
        orphaned = reap_orphaned_jobs(db)
        if orphaned:
            print(f"[startup] marked {orphaned} interrupted job(s) as failed")
    finally:
        db.close()
