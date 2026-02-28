"""Background indexing with status tracking."""

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from datetime import datetime


class IndexStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class IndexJob:
    job_id: str
    source: str  # s3_uri or path
    status: IndexStatus = IndexStatus.PENDING
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    entities_indexed: int = 0
    error: Optional[str] = None
    progress: float = 0.0  # 0-100


class BackgroundIndexer:
    """Manages background indexing jobs."""

    def __init__(self):
        self.jobs: dict[str, IndexJob] = {}
        self._lock = threading.Lock()

    def start_job(self, job_id: str, source: str, index_func, *args) -> IndexJob:
        """Start a background indexing job."""
        job = IndexJob(job_id=job_id, source=source)

        with self._lock:
            self.jobs[job_id] = job

        thread = threading.Thread(
            target=self._run_job,
            args=(job, index_func, *args),
            daemon=True
        )
        thread.start()

        return job

    def _run_job(self, job: IndexJob, index_func, *args):
        """Execute indexing in background."""
        job.status = IndexStatus.RUNNING
        job.started_at = datetime.utcnow()

        try:
            count = index_func(*args, progress_callback=lambda p: self._update_progress(job, p))
            job.entities_indexed = count
            job.status = IndexStatus.COMPLETED
        except Exception as e:
            job.status = IndexStatus.FAILED
            job.error = str(e)
        finally:
            job.completed_at = datetime.utcnow()

    def _update_progress(self, job: IndexJob, progress: float):
        """Update job progress."""
        job.progress = progress

    def get_job(self, job_id: str) -> Optional[IndexJob]:
        """Get job status."""
        return self.jobs.get(job_id)

    def list_jobs(self) -> list[IndexJob]:
        """List all jobs."""
        return list(self.jobs.values())
