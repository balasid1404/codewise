"""FastAPI service for fault localization with all improvements."""

import os
import uuid
import tempfile
import zipfile
import shutil
import logging
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
from contextlib import asynccontextmanager

from fault_localizer_prod import FaultLocalizerProd
from indexer.s3_loader import S3CodebaseLoader
from indexer.background_indexer import BackgroundIndexer, IndexStatus
from cache import QueryCache
from webhooks import GitWebhookHandler
from utils import wait_for_opensearch

# Configure logging so all loggers (including indexer, resolver) output at INFO level
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


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
            use_llm=os.getenv("USE_LLM", "false").lower() == "true",
            use_cross_encoder=os.getenv("USE_CROSS_ENCODER", "true").lower() == "true",
        )
    except Exception as e:
        print(f"Warning: Could not connect to OpenSearch: {e}")
        localizer = None

    yield

    # Cleanup
    pass


app = FastAPI(title="Fault Localization API", lifespan=lifespan)

# Serve UI
STATIC_DIR = Path(__file__).parent / "static"


@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# Request/Response Models
class IndexRequest(BaseModel):
    codebase_path: Optional[str] = None
    s3_uri: Optional[str] = None
    workers: int = 4
    incremental: bool = True  # Only index changed files
    namespace: Optional[str] = None  # Auto-derived from S3 path if not provided


class LocalizeRequest(BaseModel):
    error_text: str
    top_k: int = 5
    namespace: Optional[str] = None  # Auto-detected from stack trace if not provided


class ImageLocalizeRequest(BaseModel):
    image_path: str
    top_k: int = 5


class FaultLocation(BaseModel):
    entity_id: str = ""
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
    namespace: Optional[str] = None
    stage: str = ""
    files_parsed: int = 0
    files_total: int = 0
    entities_parsed: int = 0
    entities_embedded: int = 0


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

    # Derive namespace from S3 path or codebase path if not provided
    namespace = request.namespace
    if not namespace and request.s3_uri:
        # s3://bucket/music-backend/ → music-backend
        parts = request.s3_uri.replace("s3://", "").split("/")
        namespace = parts[1] if len(parts) > 1 and parts[1] else None
    if not namespace and request.codebase_path:
        namespace = Path(request.codebase_path).name

    def do_index(progress_callback=None):
        if request.s3_uri:
            local_path = s3_loader.download(request.s3_uri)
            try:
                count = localizer.index_codebase(str(local_path), request.workers, namespace=namespace, progress_callback=progress_callback)
            finally:
                s3_loader.cleanup(local_path)
        else:
            count = localizer.index_codebase(request.codebase_path, request.workers, namespace=namespace, progress_callback=progress_callback)

        # Invalidate cache after reindex
        if cache:
            cache.invalidate()

        return count

    background_indexer.start_job(job_id, source, do_index, namespace=namespace)

    return IndexJobResponse(
        job_id=job_id,
        status="started",
        message=f"Indexing started for {source}"
    )


@app.post("/index/upload", response_model=IndexJobResponse)
async def index_from_upload(
    file: UploadFile = File(...),
    namespace: str = Form(""),
    workers: int = Form(4),
):
    """Index codebase from uploaded zip file. Extracts to temp dir, indexes, then cleans up."""
    if not localizer:
        raise HTTPException(status_code=503, detail="OpenSearch not available")

    if not file.filename or not file.filename.endswith(".zip"):
        raise HTTPException(status_code=400, detail="Please upload a .zip file")

    # Derive namespace from zip filename if not provided
    ns = namespace.strip() or Path(file.filename).stem

    # Save uploaded zip to temp file
    with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
        content = await file.read()
        tmp.write(content)
        zip_path = tmp.name

    job_id = str(uuid.uuid4())[:8]
    source = f"upload:{file.filename}"

    def do_index(progress_callback=None):
        extract_dir = tempfile.mkdtemp(prefix="codewise_upload_")
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(extract_dir)

            # If zip contains a single top-level folder, use that
            entries = os.listdir(extract_dir)
            if len(entries) == 1 and os.path.isdir(os.path.join(extract_dir, entries[0])):
                index_path = os.path.join(extract_dir, entries[0])
            else:
                index_path = extract_dir

            count = localizer.index_codebase(index_path, workers, namespace=ns, progress_callback=progress_callback)

            if cache:
                cache.invalidate()

            return count
        finally:
            shutil.rmtree(extract_dir, ignore_errors=True)
            os.unlink(zip_path)

    background_indexer.start_job(job_id, source, do_index, namespace=ns)

    return IndexJobResponse(
        job_id=job_id,
        status="started",
        message=f"Indexing started for {file.filename} (namespace: {ns})"
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
                "entities_indexed": j.entities_indexed,
                "namespace": j.namespace,
            }
            for j in jobs
        ]
    }


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
        error=job.error,
        namespace=job.namespace,
        stage=job.stage,
        files_parsed=job.files_parsed,
        files_total=job.files_total,
        entities_parsed=job.entities_parsed,
        entities_embedded=job.entities_embedded,
    )


@app.post("/index/{job_id}/cancel")
async def cancel_index_job(job_id: str):
    """Cancel a running indexing job."""
    job = background_indexer.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status != IndexStatus.RUNNING:
        raise HTTPException(status_code=400, detail=f"Job is not running (status: {job.status.value})")

    cancelled = background_indexer.cancel_job(job_id)
    if not cancelled:
        raise HTTPException(status_code=400, detail="Could not cancel job")

    return {"job_id": job_id, "message": "Cancellation requested. Job will stop after current batch."}


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
        results = localizer.localize(request.error_text, request.top_k, namespace=request.namespace)

        locations = []
        for r in results:
            entity = r["entity"]
            confidence = r.get("confidence", r.get("score", 0))
            locations.append(FaultLocation(
                entity_id=entity.id,
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
                entity_id=entity.id,
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


@app.post("/localize/unified")
async def localize_unified(
    error_text: str = Form(""),
    file: Optional[UploadFile] = File(None),
    top_k: int = Form(5),
    namespace: str = Form(""),
):
    """Unified localization: text + optional image. Returns results and image extraction JSON."""
    if not localizer:
        raise HTTPException(status_code=503, detail="OpenSearch not available")

    if not error_text and not file:
        raise HTTPException(status_code=400, detail="Provide error text, an image, or both")

    tmp_path = None
    try:
        if file and file.filename:
            suffix = Path(file.filename).suffix or ".png"
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                content = await file.read()
                tmp.write(content)
                tmp_path = tmp.name

        result = localizer.localize_unified(
            error_text=error_text,
            image_path=tmp_path or "",
            top_k=top_k,
            namespace=namespace or None,
        )

        locations = []
        for r in result["results"]:
            entity = r["entity"]
            confidence = r.get("confidence", r.get("score", 0))
            locations.append({
                "entity_id": entity.id,
                "name": entity.name,
                "full_name": entity.full_name,
                "file_path": entity.file_path,
                "start_line": entity.start_line,
                "end_line": entity.end_line,
                "signature": entity.signature,
                "confidence": confidence,
                "confidence_label": _get_confidence_label(confidence),
                "reason": r.get("reason", ""),
            })

        return {
            "results": locations,
            "image_extracted": result["image_extracted"],
            "namespace_used": result.get("namespace_used"),
            "namespace_source": result.get("namespace_source", "none"),
            "cached": False,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if tmp_path:
            os.unlink(tmp_path)


@app.post("/localize/image/upload", response_model=LocalizeResponse)
async def localize_from_image_upload(file: UploadFile = File(...), top_k: int = Form(5), context: str = Form("")):
    """Localize fault from uploaded screenshot with optional follow-up context."""
    if not localizer:
        raise HTTPException(status_code=503, detail="OpenSearch not available")

    try:
        suffix = Path(file.filename).suffix or ".png"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            content = await file.read()
            tmp.write(content)
            tmp_path = tmp.name

        try:
            results = localizer.localize_from_image(tmp_path, top_k, context=context)
        finally:
            os.unlink(tmp_path)

        locations = []
        for r in results:
            entity = r["entity"]
            confidence = r.get("confidence", r.get("score", 0))
            locations.append(FaultLocation(
                entity_id=entity.id,
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


@app.get("/namespaces")
async def list_namespaces():
    """List all indexed namespaces with entity counts."""
    if not localizer:
        raise HTTPException(status_code=503, detail="OpenSearch not available")
    try:
        return {"namespaces": localizer.store.list_namespaces()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/namespaces/search")
async def search_namespaces(q: str = "", limit: int = 10):
    """Search namespaces by prefix for typeahead autocomplete."""
    if not localizer:
        raise HTTPException(status_code=503, detail="OpenSearch not available")
    try:
        return {"namespaces": localizer.store.search_namespaces(q, limit)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/namespaces/{namespace}")
async def delete_namespace(namespace: str):
    """Delete all indexed entities for a namespace."""
    if not localizer:
        raise HTTPException(status_code=503, detail="OpenSearch not available")
    try:
        result = localizer.store.client.delete_by_query(
            index="code_entities",
            body={"query": {"term": {"namespace": namespace}}}
        )
        deleted = result.get("deleted", 0)
        return {"namespace": namespace, "deleted": deleted, "message": f"Deleted {deleted} entities from namespace '{namespace}'"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/dependencies/{entity_id}")
async def get_dependencies(entity_id: str, namespace: str = None):
    """Get dependency tree for an entity: calls, callers, imports, same-file siblings."""
    if not localizer:
        raise HTTPException(status_code=503, detail="OpenSearch not available")
    try:
        result = localizer.store.get_dependencies(entity_id, namespace=namespace)
        if "error" in result:
            raise HTTPException(status_code=404, detail=result["error"])
        return result
    except HTTPException:
        raise
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


@app.post("/index/bulk-import")
async def bulk_import_entities(request: Request):
    """Import pre-embedded entities from local indexing. Accepts JSON array of entity dicts."""
    if not localizer:
        raise HTTPException(status_code=503, detail="OpenSearch not available")

    data = await request.json()
    entities_data = data.get("entities", [])
    namespace = data.get("namespace", "default")

    if not entities_data:
        raise HTTPException(status_code=400, detail="No entities provided")

    from indexer.entities import CodeEntity, EntityType

    entities = []
    for ed in entities_data:
        ent = CodeEntity(
            id=ed["id"],
            name=ed["name"],
            entity_type=EntityType(ed["entity_type"]),
            file_path=ed["file_path"],
            start_line=ed["start_line"],
            end_line=ed["end_line"],
            signature=ed["signature"],
            body=ed.get("body", ""),
            class_name=ed.get("class_name"),
            package=ed.get("package"),
            docstring=ed.get("docstring"),
            embedding=ed.get("embedding"),
            calls=ed.get("calls", []),
            imports=ed.get("imports", []),
            annotations=ed.get("annotations", []),
            namespace=namespace,
            resolved_calls=ed.get("resolved_calls", []),
            base_classes=ed.get("base_classes", []),
            file_imports=ed.get("file_imports", []),
            references=ed.get("references", []),
        )
        entities.append(ent)

    count = localizer.store.index(entities)
    return {"imported": count, "namespace": namespace}


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
