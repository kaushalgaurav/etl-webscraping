"""Alembic environment.

The database URL and the model metadata both come from the application, so a
migration can never run against a different database than the app, and a new
model is picked up without editing anything here.

Only the ORM tables are under migration control — datasets, sources, jobs,
profiles, kpi_requests. The warehouse layers (census_raw, hmis_master,
dashboard_master, ds_*) are deliberately excluded: their columns are decided
at ingest time by the data itself, so a fixed migration could not describe
them. `include_object` below enforces that split.
"""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import settings
from app.db.base import Base
from app import models  # noqa: F401  (registers every table on Base.metadata)

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Tables the app creates dynamically from whatever the source supplies.
DATA_TABLE_PREFIXES = ("census_", "hmis_", "dashboard_", "ds_")


def include_object(obj, name, type_, reflected, compare_to):
    """Keep autogenerate away from the dynamically-shaped data tables.

    Without this, `alembic revision --autogenerate` would see census_raw and
    friends in the database, find no model for them, and write a migration
    that drops them.
    """
    if type_ == "table" and name.startswith(DATA_TABLE_PREFIXES):
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        include_object=include_object,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
