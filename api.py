"""FastAPI service for fault localization with all improvements."""

import os
import uuid
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from pydantic import BaseModel
from typing import Optional
from contextlib import asynccontextmanager

from fault_localizer_prod import FaultLocalizerProd
from indexer.s3_loader import S3CodebaseLoader
from indexer.background_indexer import BackgroundIndexer, IndexStatus
from cache import QueryCache
from webhooks import GitWebhookHandler
from utils import wait_for_opensearch


# Global instances
localizer: Optional[FaultLocalizerProd] = None
s3_loader: Optional[S3CodebaseLoader] = None
background_indexer: Optional[BackgroundIndexer] = None
cache: Optional[QueryCache] = None
webhook_handler: Optional[GitWebhookHandler] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize services on startup."""
    global localizer, s3_loader, background_indexer, cache, webhook_handler

    # Wait for OpenSearch if configured
    opensearch_host = os.getenv("OPENSEARCH_HOST", "localhost")

    s3_loader = S3CodebaseLoader(region=os.getenv("AWS_REGION", "us-east-1"))
    background_indexer = BackgroundIndexer()
    cache = QueryCache(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", "6379"))
    )
    webhook_handler = GitWebhookHandler(secret=os.getenv("WEBHOOK_SECRET"))

    try:
        localizer = FaultLocalizerProd(
            opensearch_host=opensearch_host,
            opensearch_port=int(os.getenv("OPENSEARCH_PORT", "9200")),
            use_llm=os.getenv("USE_LLM", "false").lower() == "true"
        )
    except Exception as e:
        print(f"Warning: Could not connect to OpenSearch: {e}")
        localizer = None

    yield

    # Cleanup
    pass


app = FastAPI(title="Fault Localization API", lifespan=lifespan)


# Request/Response Models
class IndexRequest(BaseModel):
    codebase_path: Optional[str] = None
    s3_uri: Optional[str] = None
    workers: int = 4
    incremental: bool = True  # Only index changed files


class LocalizeRequest(BaseModel):
    error_text: str
    top_k: int = 5


class ImageLocalizeRequest(BaseModel):
    image_path: str
    top_k: int = 5


class FaultLocation(BaseModel):
    name: str
    full_name: str
    file_path: str
    start_line: int
    end_line: int
    signature: str
    confidence: float
    confidence_label: str = ""
    reason: str


class LocalizeResponse(BaseModel):
    results: list[FaultLocation]
    cached: bool = False


class IndexJobResponse(BaseModel):
    job_id: str
    status: str
    message: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    progress: float
    entities_indexed: int
    error: Optional[str] = None


# Endpoints
@app.post("/index", response_model=IndexJobResponse)
async def index_codebase(request: IndexRequest):
    """Start background indexing job."""
    if not localizer:
        raise HTTPException(status_code=503, detail="OpenSearch not available")

    if not request.s3_uri and not request.codebase_path:
        raise HTTPException(status_code=400, detail="Provide codebase_path or s3_uri")

    job_id = str(uuid.uuid4())[:8]
    source = request.s3_uri or request.codebase_path

    def do_index(progress_callback=None):
        if request.s3_uri:
            local_path = s3_loader.download(request.s3_uri)
            try:
                count = localizer.index_codebase(str(local_path), request.workers)
            finally:
                s3_loader.cleanup(local_path)
        else:
            count = localizer.index_codebase(request.codebase_path, request.workers)

        # Invalidate cache after reindex
        if cache:
            cache.invalidate()

        return count

    background_indexer.start_job(job_id, source, do_index)

    return IndexJobResponse(
        job_id=job_id,
        status="started",
        message=f"Indexing started for {source}"
    )


@app.get("/index/{job_id}", response_model=JobStatusResponse)
async def get_index_status(job_id: str):
    """Get indexing job status."""
    job = background_indexer.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    return JobStatusResponse(
        job_id=job.job_id,
        status=job.status.value,
        progress=job.progress,
        entities_indexed=job.entities_indexed,
        error=job.error
    )


@app.get("/index/jobs/list")
async def list_index_jobs():
    """List all indexing jobs."""
    jobs = background_indexer.list_jobs()
    return {
        "jobs": [
            {
                "job_id": j.job_id,
                "source": j.source,
                "status": j.status.value,
                "progress": j.progress,
                "entities_indexed": j.entities_indexed
            }
            for j in jobs
        ]
    }


@app.post("/localize", response_model=LocalizeResponse)
async def localize_fault(request: LocalizeRequest):
    """Localize fault from stack trace."""
    if not localizer:
        raise HTTPException(status_code=503, detail="OpenSearch not available")

    # Check cache
    if cache:
        cached_results = cache.get(request.error_text, "localize")
        if cached_results:
            return LocalizeResponse(
                results=[FaultLocation(**r) for r in cached_results],
                cached=True
            )

    try:
        results = localizer.localize(request.error_text, request.top_k)

        locations = []
        for r in results:
            entity = r["entity"]
            confidence = r.get("confidence", r.get("score", 0))
            locations.append(FaultLocation(
                name=entity.name,
                full_name=entity.full_name,
                file_path=entity.file_path,
                start_line=entity.start_line,
                end_line=entity.end_line,
                signature=entity.signature,
                confidence=confidence,
                confidence_label=_get_confidence_label(confidence),
                reason=r.get("reason", "")
            ))

        # Cache results
        if cache and locations:
            cache.set(request.error_text, [l.model_dump() for l in locations], "localize")

        return LocalizeResponse(results=locations, cached=False)

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/localize/image", response_model=LocalizeResponse)
async def localize_from_image(request: ImageLocalizeRequest):
    """Localize fault from screenshot."""
    if not localizer:
        raise HTTPException(status_code=503, detail="OpenSearch not available")

    try:
        results = localizer.localize_from_image(request.image_path, request.top_k)

        locations = []
        for r in results:
            entity = r["entity"]
            confidence = r.get("confidence", r.get("score", 0))
            locations.append(FaultLocation(
                name=entity.name,
                full_name=entity.full_name,
                file_path=entity.file_path,
                start_line=entity.start_line,
                end_line=entity.end_line,
                signature=entity.signature,
                confidence=confidence,
                confidence_label=_get_confidence_label(confidence),
                reason=r.get("reason", "")
            ))

        return LocalizeResponse(results=locations, cached=False)

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/webhook/github")
async def github_webhook(request: Request, background_tasks: BackgroundTasks):
    """Handle GitHub push webhook for auto-reindex."""
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")

    if not webhook_handler.verify_github_signature(body, signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = await request.json()
    event = webhook_handler.parse_github_push(payload)

    if webhook_handler.should_reindex(event["changed_files"]):
        # Trigger background reindex
        s3_uri = os.getenv("CODEBASE_S3_URI")
        if s3_uri:
            background_tasks.add_task(
                background_indexer.start_job,
                f"webhook-{uuid.uuid4()[:8]}",
                s3_uri,
                lambda: localizer.index_codebase(s3_uri, workers=4)
            )

    return {
        "received": True,
        "repo": event["repo"],
        "changed_files": len(event["changed_files"]),
        "will_reindex": webhook_handler.should_reindex(event["changed_files"])
    }


@app.get("/health")
async def health():
    """Health check endpoint."""
    opensearch_ok = False
    if localizer:
        try:
            localizer.store.client.cluster.health()
            opensearch_ok = True
        except Exception:
            pass

    return {
        "status": "healthy" if opensearch_ok else "degraded",
        "opensearch": "connected" if opensearch_ok else "disconnected",
        "cache": "enabled" if cache and cache.enabled else "disabled"
    }


@app.get("/stats")
async def stats():
    """Get system statistics."""
    if not localizer:
        return {"error": "OpenSearch not available"}

    try:
        entity_count = localizer.store.client.count(index="code_entities")["count"]
    except Exception:
        entity_count = 0

    return {
        "entities_indexed": entity_count,
        "cache_enabled": cache.enabled if cache else False,
        "active_jobs": len([j for j in background_indexer.list_jobs() if j.status == IndexStatus.RUNNING])
    }


def _get_confidence_label(score: float) -> str:
    """Get human-readable confidence label."""
    if score >= 0.8:
        return "high"
    elif score >= 0.5:
        return "medium"
    elif score >= 0.3:
        return "low"
    return "very_low"
