"""Application entry point.

Thin by design: creates the app, wires middleware and lifecycle, and mounts
the aggregated router. All behaviour lives in app/modules/<section>/.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.db.init_db import init_db
from app.modules.jobs.service import shutdown as shutdown_jobs
from app.routes import api_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield
    shutdown_jobs()


app = FastAPI(title="Data Ingestion & Analytics Platform", version="0.2.0", lifespan=lifespan,root_path="/etl",)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # local MVP; tighten before any real deployment
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok"}
