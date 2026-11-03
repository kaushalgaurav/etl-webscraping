"""Engine and session factory.

One database holds both metadata tables (datasets, sources, jobs) and the
per-dataset data tables written by pandas.to_sql. SQLite by default;
PostgreSQL when PLATFORM_DATABASE_URL is set (see .env.example).
"""
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

if settings.database_url.startswith("sqlite"):
    engine = create_engine(
        settings.database_url,
        # check_same_thread=False: FastAPI's threadpool and the job workers serve
        # requests off-thread.
        # timeout=30: SQLite busy-retry window, since the ORM session (metadata
        # writes) and pandas.to_sql (dataset table writes) can briefly overlap;
        # without this a second writer fails immediately with "database is locked".
        connect_args={"check_same_thread": False, "timeout": 30},
    )
else:
    # PostgreSQL (or another server DB). pool_pre_ping drops connections the
    # server closed while idle — long job runs otherwise hit stale sockets.
    engine = create_engine(settings.database_url, pool_pre_ping=True)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
