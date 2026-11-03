"""Resolve a job id to the datasets it produced.

A plain fetch job points straight at one dataset. A crawl job produces none of
its own — it queues a child job per branch and records them in
`result["children"]` — so exporting "what this job fetched" has to follow that
tree rather than read the parent's own (empty) dataset_id.
"""
from sqlalchemy.orm import Session

from app.models import Job

MAX_DEPTH = 5  # crawl trees are shallow; this only guards against a cycle


def dataset_ids_for_job(db: Session, job_id: str, _depth: int = 0) -> list[str]:
    """Every dataset produced by this job, including its crawl children."""
    job = db.get(Job, job_id)
    if job is None:
        raise ValueError(f"Job '{job_id}' not found.")

    found: list[str] = []
    if job.dataset_id:
        found.append(job.dataset_id)

    if _depth < MAX_DEPTH:
        for child in (job.result or {}).get("children") or []:
            child_id = child.get("job_id") if isinstance(child, dict) else child
            if child_id:
                found.extend(dataset_ids_for_job(db, child_id, _depth + 1))

    # dict.fromkeys keeps first-seen order while removing repeats.
    return list(dict.fromkeys(found))
