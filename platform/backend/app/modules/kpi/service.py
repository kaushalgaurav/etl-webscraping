"""KPI service.

Claude proposes *which* computations to run; this module executes them via the
deterministic analytics engine. The LLM never touches the data or the DB, so
results stay reproducible and auditable.
"""
import pandas as pd

from app.modules.analytics.service import run_computation


def execute_proposal(df: pd.DataFrame, proposal: dict) -> list[dict]:
    """Run every KPI computation Claude proposed against the dataset."""
    executed = []
    for kpi in proposal.get("kpis", []):
        outcome = run_computation(df, kpi.get("computation", {}))
        executed.append({
            "name": kpi.get("name"),
            "description": kpi.get("description"),
            "computation": kpi.get("computation"),
            "result": outcome.get("result"),
            "error": outcome.get("error"),
        })
    return executed
