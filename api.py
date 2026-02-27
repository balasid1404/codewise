"""FastAPI service for fault localization."""

import os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from fault_localizer_prod import FaultLocalizerProd
from indexer.s3_loader import S3CodebaseLoader

app = FastAPI(title="Fault Localization API")

localizer = FaultLocalizerProd(
    opensearch_host=os.getenv("OPENSEARCH_HOST", "localhost"),
    opensearch_port=int(os.getenv("OPENSEARCH_PORT", "9200")),
    use_llm=os.getenv("USE_LLM", "true").lower() == "true"
)

s3_loader = S3CodebaseLoader(region=os.getenv("AWS_REGION", "us-east-1"))


class IndexRequest(BaseModel):
    codebase_path: str = None
    s3_uri: str = None  # s3://bucket/prefix/
    workers: int = 4


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
    reason: str


class LocalizeResponse(BaseModel):
    results: list[FaultLocation]


@app.post("/index")
def index_codebase(request: IndexRequest):
    try:
        if request.s3_uri:
            # Download from S3
            local_path = s3_loader.download(request.s3_uri)
            count = localizer.index_codebase(str(local_path), request.workers)
            s3_loader.cleanup(local_path)
        elif request.codebase_path:
            count = localizer.index_codebase(request.codebase_path, request.workers)
        else:
            raise HTTPException(status_code=400, detail="Provide codebase_path or s3_uri")
        
        return {"indexed": count, "status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/localize", response_model=LocalizeResponse)
def localize_fault(request: LocalizeRequest):
    try:
        results = localizer.localize(request.error_text, request.top_k)
        locations = []
        for r in results:
            entity = r["entity"]
            locations.append(FaultLocation(
                name=entity.name,
                full_name=entity.full_name,
                file_path=entity.file_path,
                start_line=entity.start_line,
                end_line=entity.end_line,
                signature=entity.signature,
                confidence=r.get("confidence", r.get("score", 0)),
                reason=r.get("reason", "")
            ))
        return LocalizeResponse(results=locations)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/localize/image", response_model=LocalizeResponse)
def localize_from_image(request: ImageLocalizeRequest):
    """Localize fault from a screenshot (no stack trace needed)."""
    try:
        results = localizer.localize_from_image(request.image_path, request.top_k)
        locations = []
        for r in results:
            entity = r["entity"]
            locations.append(FaultLocation(
                name=entity.name,
                full_name=entity.full_name,
                file_path=entity.file_path,
                start_line=entity.start_line,
                end_line=entity.end_line,
                signature=entity.signature,
                confidence=r.get("confidence", r.get("score", 0)),
                reason=r.get("reason", "")
            ))
        return LocalizeResponse(results=locations)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
def health():
    return {"status": "healthy"}
