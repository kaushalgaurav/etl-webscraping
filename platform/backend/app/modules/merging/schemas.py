"""Merge request models."""
from pydantic import BaseModel, Field, model_validator


class MergeRequest(BaseModel):
    """What to merge: either a survey across connectors, or explicit datasets.

    `survey_id` + `connectors` is the usual way — one survey's census and HMIS
    data joined on their shared geography. Always the master layer: raw and
    clean hold one row per pull, so merging them would multiply rows, while
    master is already one row per place. `dataset_ids` stays for merging two
    specific datasets unrelated to a survey.
    """

    survey_id: int | None = Field(
        default=None,
        description="Merge this survey's rows. The survey is what links the "
                    "sources across warehouses.",
    )
    connectors: list[str] | None = Field(
        default=None,
        description="Which warehouses to merge for the survey, e.g. ['census','hmis']. "
                    "Omit to use every warehouse holding that survey.",
        examples=[["census", "hmis"]],
    )
    dataset_ids: list[str] | None = Field(
        default=None,
        description="Explicit datasets to merge, instead of survey_id + connectors.",
    )
    web_tables: list[str] | None = Field(
        default=None,
        description="Website tables to merge (their web_table_id from GET /warehouse/web/tables). "
                    "Needs survey_id. They join HMIS on district and year, Census on district.",
    )
    join_key: str | None = Field(
        default=None,
        description="Column or canonical field name (e.g. 'district'); resolved per dataset.",
    )
    name: str | None = None

    @model_validator(mode="after")
    def _one_way_or_the_other(self):
        if not self.survey_id and not self.dataset_ids:
            raise ValueError("Pass survey_id (with optional connectors) or dataset_ids.")
        if self.web_tables and not self.survey_id:
            raise ValueError("web_tables needs survey_id.")
        return self

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"survey_id": 1, "connectors": ["census", "hmis"]},
                {"survey_id": 1},
                {"dataset_ids": ["<id a>", "<id b>"], "join_key": "district"},
            ]
        }
    }


class DashboardCleanRequest(BaseModel):
    """Re-clean the rows already in dashboard_master."""

    survey_id: int | None = Field(
        default=None,
        description="Only re-clean this survey's rows. Omit to re-clean every "
                    "survey in the table.",
        examples=[1],
    )
    operations: list[str] | None = Field(
        default=None,
        description="Cleaning operations in order. Omit for the standard warehouse "
                    "set. Available: standardize_columns, trim_whitespace, "
                    "normalize_nulls, drop_empty_columns, drop_empty_rows, "
                    "coerce_numeric, drop_duplicates, standardize_case, "
                    "standardize_states.",
        examples=[["standardize_columns", "trim_whitespace", "coerce_numeric"]],
    )
    name: str | None = Field(
        default=None, description="Dataset name for the audit row; auto-generated if omitted."
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"survey_id": 1},
                {},
                {"survey_id": 1, "operations": ["trim_whitespace", "coerce_numeric"]},
            ]
        }
    }


