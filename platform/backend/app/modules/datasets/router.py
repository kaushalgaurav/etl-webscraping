"""Dataset browsing endpoints."""
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.crud import dataset as dataset_crud
from app.db.session import get_db
from app.helpers.dataframe import json_safe_records
from app.models import Dataset
from app.schemas import DatasetOut

router = APIRouter(prefix="/datasets", tags=["datasets"])


@router.get("", response_model=list[DatasetOut])
def list_datasets(stage: str | None = None, db: Session = Depends(get_db)):
    stmt = select(Dataset).order_by(Dataset.created_at.desc())
    if stage:
        stmt = stmt.where(Dataset.stage == stage)
    return db.execute(stmt).scalars().all()


@router.get("/{dataset_id}", response_model=DatasetOut)
def get_dataset(dataset_id: str, db: Session = Depends(get_db)):
    return dataset_crud.get_dataset_or_404(db, dataset_id)


@router.get("/{dataset_id}/rows")
def get_rows(dataset_id: str, limit: int = 50, offset: int = 0, db: Session = Depends(get_db)):
    dataset = dataset_crud.get_dataset_or_404(db, dataset_id)
    total, page = dataset_crud.load_page(dataset, limit, offset)
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "rows": json_safe_records(page),
    }
