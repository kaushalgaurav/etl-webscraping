"""CLAUDE integration: turns a natural-language KPI prompt + dataset schema
into a structured set of KPI computations plus a narrative.

Claude never touches the data directly. It's given the schema, a small
profile summary, and a sample of rows, and asked to return *which*
computations to run (see app.modules.analytics.service.run_computation for the
fixed vocabulary of computation types). The backend then executes those specs
itself. This keeps the LLM in charge of interpretation/wording while keeping
actual data access deterministic and auditable.

The shape is enforced with structured outputs rather than a forced tool call:
the tool only ever existed to get JSON back, and forcing a call
(`tool_choice={"type": "tool", ...}`) is rejected with a 400 on the current
models. `messages.parse()` validates the reply against the models below, so a
malformed proposal fails here instead of inside the executor.
"""
import json
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from app.core.config import settings

# Mirrors the vocabulary of app.modules.analytics.service.run_computation.
# Every field is required but nullable rather than optional: a structured
# output schema lists each property in `required`, so the model fills the
# fields its chosen `type` does not use with null instead of omitting them.
AGG_FUNCTIONS = ("sum", "mean", "count", "min", "max", "median", "nunique")


class Computation(BaseModel):
    type: Literal["aggregate", "top_n", "correlation", "describe"]
    group_by: str | None = Field(description="aggregate: column to group on.")
    agg: Literal[AGG_FUNCTIONS] | None = Field(  # type: ignore[valid-type]
        description="aggregate: which aggregation to apply."
    )
    metric: str | None = Field(description="aggregate: numeric column to aggregate.")
    column: str | None = Field(description="top_n / describe: the column to use.")
    n: int | None = Field(description="top_n: how many rows to return.")
    ascending: bool | None = Field(description="top_n: true for the smallest values.")
    column_a: str | None = Field(description="correlation: first column.")
    column_b: str | None = Field(description="correlation: second column.")


class Kpi(BaseModel):
    name: str
    description: str
    computation: Computation


class KpiProposal(BaseModel):
    narrative: str = Field(
        description="2-4 sentence plain-language summary of the key insight(s)."
    )
    kpis: list[Kpi] = Field(description="Up to 8 KPIs, each with a runnable computation.")


SYSTEM_PROMPT = (
    "You propose KPIs over an Indian public-health dataset. Return a small set "
    "of KPIs that answer the user's request, each backed by one concrete, "
    "executable computation.\n\n"
    "Rules:\n"
    "- Column names must match the given schema exactly, character for character.\n"
    "- Choose numeric columns for `metric`, `column_a`/`column_b` and for "
    "aggregations other than count/nunique.\n"
    "- Set the fields your chosen computation type uses and leave the rest null.\n"
    "- At most 8 KPIs. Prefer a few meaningful ones over many trivial ones.\n"
    "- Columns suffixed `_census` come from the census (denominators, population); "
    "`_hmis` columns are health-service measures. A rate usually means an `_hmis` "
    "measure against a `_census` denominator."
)


class ClaudeNotConfigured(Exception):
    pass


def propose_kpis(prompt: str, profile: dict, sample_rows: list[dict]) -> dict:
    if not settings.anthropic_api_key:
        raise ClaudeNotConfigured(
            "ANTHROPIC_API_KEY is not set. Set it in the backend's environment "
            "to enable KPI generation."
        )

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    columns_summary = [
        {
            "name": c["name"],
            "dtype": c["dtype"],
            "null_pct": c["null_pct"],
            "unique_count": c["unique_count"],
            "sample_values": c["sample_values"],
        }
        for c in profile["columns"]
    ]

    user_content = (
        f"Dataset has {profile['row_count']} rows and {profile['column_count']} columns.\n\n"
        f"Columns:\n{json.dumps(columns_summary, indent=2, default=str)}\n\n"
        f"Sample rows:\n{json.dumps(sample_rows, indent=2, default=str)}\n\n"
        f"User's KPI request: {prompt}"
    )

    response = client.messages.parse(
        model=settings.claude_model,
        max_tokens=8000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
        output_format=KpiProposal,
    )

    # A policy decline comes back as HTTP 200 with no parsed output, so the
    # stop reason is checked before the result is read.
    if response.stop_reason == "refusal":
        detail = getattr(response.stop_details, "explanation", None) or "no explanation given"
        raise RuntimeError(f"Claude declined this request ({detail}).")

    proposal = response.parsed_output
    if proposal is None:
        raise RuntimeError(
            f"Claude returned no usable KPI proposal (stop_reason={response.stop_reason})."
        )

    # Dropping the nulls keeps the executor seeing only the keys a computation
    # actually uses, exactly as the old hand-built tool schema delivered them.
    return {
        "narrative": proposal.narrative,
        "kpis": [
            {
                "name": k.name,
                "description": k.description,
                "computation": {
                    key: value
                    for key, value in k.computation.model_dump().items()
                    if value is not None
                },
            }
            for k in proposal.kpis
        ],
    }
