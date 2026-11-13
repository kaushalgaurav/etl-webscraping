"""Aggregates every module's router into one API router.

Order matters: merging's /datasets/merge routes must be registered before
datasets' /datasets/{dataset_id}, or "merge" would be captured as an id.
"""
from fastapi import APIRouter

from app.modules.ai_data.router import router as ai_data_router
from app.modules.datasets.router import router as datasets_router
from app.modules.ingestion.router import router as ingestion_router
from app.modules.jobs.router import router as jobs_router
from app.modules.kpi.router import router as kpi_router
from app.modules.merging.router import router as merging_router
from app.modules.reports.router import router as reports_router
from app.modules.scraping.router import router as scraping_router
from app.modules.sources.router import router as sources_router
from app.modules.warehouse.router import router as warehouse_router

api_router = APIRouter()

api_router.include_router(ingestion_router)
api_router.include_router(warehouse_router)
api_router.include_router(sources_router)
api_router.include_router(jobs_router)
api_router.include_router(ai_data_router)
api_router.include_router(scraping_router)
# Static /datasets/... paths first, parameterized last.
api_router.include_router(merging_router)
api_router.include_router(reports_router)
api_router.include_router(kpi_router)
api_router.include_router(datasets_router)

__all__ = ["api_router"]
