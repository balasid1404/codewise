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
    CANCELLED = "cancelled"


class CancelledError(Exception):
    """Raised when a job is cancelled."""
    pass


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
    namespace: Optional[str] = None
    # Stage-level progress
    stage: str = ""  # current stage: parsing, resolving, embedding, indexing
    files_parsed: int = 0
    files_total: int = 0
    entities_parsed: int = 0
    entities_embedded: int = 0
    # Cancellation support
    _cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)


class BackgroundIndexer:
    """Manages background indexing jobs."""

    def __init__(self):
        self.jobs: dict[str, IndexJob] = {}
        self._lock = threading.Lock()

    def start_job(self, job_id: str, source: str, index_func, *args, namespace: str = None) -> IndexJob:
        """Start a background indexing job."""
        job = IndexJob(job_id=job_id, source=source, namespace=namespace)

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

        def progress_callback(stage, **kwargs):
            # Check for cancellation on every progress tick
            if job._cancel_event.is_set():
                raise CancelledError("Job cancelled by user")
            job.stage = stage
            if "files_total" in kwargs:
                job.files_total = kwargs["files_total"]
            if "files_parsed" in kwargs:
                job.files_parsed = kwargs["files_parsed"]
            if "entities_parsed" in kwargs:
                job.entities_parsed = kwargs["entities_parsed"]
            if "entities_embedded" in kwargs:
                job.entities_embedded = kwargs["entities_embedded"]
            if "entities_indexed" in kwargs:
                job.entities_indexed = kwargs["entities_indexed"]
            if "entities_total" in kwargs and kwargs["entities_total"] > 0:
                job.progress = kwargs.get("entities_indexed", 0) / kwargs["entities_total"]

        try:
            count = index_func(*args, progress_callback=progress_callback)
            job.entities_indexed = count
            job.progress = 1.0
            job.status = IndexStatus.COMPLETED
        except CancelledError:
            job.status = IndexStatus.CANCELLED
            job.error = "Cancelled by user"
        except Exception as e:
            if job._cancel_event.is_set():
                job.status = IndexStatus.CANCELLED
                job.error = "Cancelled by user"
            else:
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

    def cancel_job(self, job_id: str) -> bool:
        """Cancel a running job. Returns True if cancellation was signalled."""
        job = self.jobs.get(job_id)
        if not job:
            return False
        if job.status != IndexStatus.RUNNING:
            return False
        job._cancel_event.set()
        return True

    def list_jobs(self) -> list[IndexJob]:
        """List all jobs."""
        return list(self.jobs.values())
