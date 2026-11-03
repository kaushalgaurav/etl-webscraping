"""Read rows out of a shared warehouse layer table.

`GET /datasets/{id}/rows` reads one dataset. The warehouse layers
(census_raw, hmis_raw, …) instead hold every dataset stacked together, so
browsing them needs its own reader: paged, filterable, and scopeable to the
job that produced a slice of the rows.

Paging is done in SQL. These tables run to millions of rows, so loading one
into a DataFrame to slice it would exhaust memory for a 50-row preview.
"""
import pandas as pd
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

MAX_LIMIT = 1000


class LayerReadError(Exception):
    pass


def _quote(conn, name: str) -> str:
    return conn.dialect.identifier_preparer.quote(name)


def read_rows(
    db: Session,
    layer: str,
    *,
    allowed_layers: tuple[str, ...],
    dataset_col: str,
    limit: int = 50,
    offset: int = 0,
    dataset_ids: list[str] | None = None,
    filters: dict[str, str] | None = None,
    count: bool = True,
    order_by: list[str] | None = None,
) -> dict:
    """One page of a layer, with every column.

    `filters` and `order_by` are matched against real column names only —
    anything else is rejected rather than interpolated, so a query string can
    never reach the SQL as an identifier.

    Paging needs `order_by`. SQL gives no row order without ORDER BY, and
    PostgreSQL really does return these tables differently between calls, so
    LIMIT/OFFSET alone makes pages overlap and skip rows. When the caller
    names no ordering, the layer's own key columns are used, which keeps
    paging reproducible at the cost of a sort.
    """
    if layer not in allowed_layers:
        raise LayerReadError(
            f"Unknown layer '{layer}'. Use one of: {', '.join(allowed_layers)}"
        )

    conn = db.connection()
    if not inspect(conn).has_table(layer):
        raise LayerReadError(f"{layer} does not exist yet — build it first.")

    columns = [c["name"] for c in inspect(conn).get_columns(layer)]
    limit = max(1, min(int(limit), MAX_LIMIT))
    offset = max(0, int(offset))

    clauses: list[str] = []
    params: dict = {}

    if dataset_ids:
        names = ", ".join(f":d{i}" for i in range(len(dataset_ids)))
        clauses.append(f"{_quote(conn, dataset_col)} IN ({names})")
        params |= {f"d{i}": v for i, v in enumerate(dataset_ids)}

    for key, value in (filters or {}).items():
        if key not in columns:
            raise LayerReadError(
                f"Unknown column '{key}'. Available: {', '.join(columns[:25])}"
                + (" …" if len(columns) > 25 else "")
            )
        clauses.append(f"{_quote(conn, key)} = :f_{key}")
        params[f"f_{key}"] = value

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    quoted = _quote(conn, layer)

    # Default to the dataset column plus the first real columns: enough to make
    # the order deterministic without sorting on all 100+ of them.
    if order_by:
        unknown = [c for c in order_by if c not in columns]
        if unknown:
            raise LayerReadError(
                f"Cannot order by unknown column(s): {', '.join(unknown)}."
            )
        sort_columns = order_by
    else:
        sort_columns = [c for c in (dataset_col, *columns) if c in columns][:4]

    order = f" ORDER BY {', '.join(_quote(conn, c) for c in sort_columns)}" if sort_columns else ""

    total = None
    if count:
        total = conn.execute(text(f"SELECT count(*) FROM {quoted}{where}"), params).scalar_one()

    page = pd.read_sql_query(
        text(f"SELECT * FROM {quoted}{where}{order} LIMIT :_limit OFFSET :_offset"),
        conn, params={**params, "_limit": limit, "_offset": offset},
    )

    return {
        "layer": layer,
        "total": int(total) if total is not None else None,
        "limit": limit,
        "offset": offset,
        "returned": int(page.shape[0]),
        "columns": list(page.columns),
        "rows": page.astype(object).where(pd.notnull(page), None).to_dict(orient="records"),
    }


def read_rows_wide(
    db: Session,
    layer: str,
    *,
    allowed_layers: tuple[str, ...],
    dataset_col: str,
    index_columns: tuple[str, ...],
    variable_col: str = "variable",
    value_col: str = "value",
    limit: int = 50,
    offset: int = 0,
    dataset_ids: list[str] | None = None,
    filters: dict[str, str] | None = None,
    count: bool = True,
) -> dict:
    """Read a *long* layer as one row per record, indicators as columns.

    hmis_raw and hmis_clean store one row per indicator, so a single place-year
    spans hundreds of rows. This returns them in the same shape as hmis_master
    — one row per record — without changing what is stored: raw stays exactly
    as received.

    Pivoted in SQL with one MAX(CASE ...) per indicator and paged on the
    grouped result, so a page really is N records rather than N indicator rows.
    """
    if layer not in allowed_layers:
        raise LayerReadError(
            f"Unknown layer '{layer}'. Use one of: {', '.join(allowed_layers)}"
        )

    conn = db.connection()
    if not inspect(conn).has_table(layer):
        raise LayerReadError(f"{layer} does not exist yet — build it first.")

    columns = [c["name"] for c in inspect(conn).get_columns(layer)]
    for required in (variable_col, value_col):
        if required not in columns:
            raise LayerReadError(
                f"{layer} has no '{required}' column, so it is not a long table — "
                "read it with shape=long."
            )

    index = [c for c in index_columns if c in columns]
    if not index:
        raise LayerReadError(f"{layer} has none of the index columns {index_columns}.")

    limit = max(1, min(int(limit), MAX_LIMIT))
    offset = max(0, int(offset))

    clauses = [f"{_quote(conn, variable_col)} IS NOT NULL"]
    params: dict = {}

    if dataset_ids:
        names = ", ".join(f":d{i}" for i in range(len(dataset_ids)))
        clauses.append(f"{_quote(conn, dataset_col)} IN ({names})")
        params |= {f"d{i}": v for i, v in enumerate(dataset_ids)}

    for key, value in (filters or {}).items():
        if key not in columns:
            raise LayerReadError(
                f"Unknown column '{key}'. Available: {', '.join(columns[:25])}"
                + (" …" if len(columns) > 25 else "")
            )
        clauses.append(f"{_quote(conn, key)} = :f_{key}")
        params[f"f_{key}"] = value

    where = f" WHERE {' AND '.join(clauses)}"
    quoted = _quote(conn, layer)
    index_sql = ", ".join(_quote(conn, c) for c in index)

    total = None
    if count:
        total = conn.execute(text(
            f"SELECT count(*) FROM (SELECT DISTINCT {index_sql} FROM {quoted}{where}) t"
        ), params).scalar_one()

    variables = [
        r[0] for r in conn.execute(text(
            f"SELECT DISTINCT {_quote(conn, variable_col)} FROM {quoted}{where} ORDER BY 1"
        ), params)
    ]
    if not variables:
        raise LayerReadError(f"{layer} has no mapped indicators to pivot.")

    cases = ", ".join(
        f"MAX(CASE WHEN {_quote(conn, variable_col)} = :v{i} THEN {_quote(conn, value_col)} END) "
        f"AS {_quote(conn, v)}"
        for i, v in enumerate(variables)
    )
    params |= {f"v{i}": v for i, v in enumerate(variables)}

    page = pd.read_sql_query(
        text(
            f"SELECT {index_sql}, {cases} FROM {quoted}{where} "
            f"GROUP BY {index_sql} ORDER BY {index_sql} LIMIT :_limit OFFSET :_offset"
        ),
        conn, params={**params, "_limit": limit, "_offset": offset},
    )

    return {
        "layer": layer,
        "shape": "wide",
        "total": int(total) if total is not None else None,
        "limit": limit,
        "offset": offset,
        "returned": int(page.shape[0]),
        "indicator_columns": len(variables),
        "columns": list(page.columns),
        "rows": page.astype(object).where(pd.notnull(page), None).to_dict(orient="records"),
    }
