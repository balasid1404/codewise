# CodeWise — Benchmark Results

**Date:** March 8, 2026
**Environment:** AWS Fargate (1 vCPU, 6 GB RAM), self-hosted OpenSearch 2.11 (0.5 vCPU, 1 GB), us-east-1
**Codebase:** fault-localization (471 entities, Python/JS/TS/HTML)
**LLM:** Amazon Nova Pro v1:0 via Bedrock Converse API
**Embeddings:** CodeBERT (microsoft/codebert-base, 768-dim)

---

## 1. Indexing Performance

| Metric | Value |
|--------|-------|
| Source | S3 (`s3://fault-loc-codebase-650251724071/fault-localization/`) |
| Files parsed | Python, Java, JS, TS, HTML |
| Entities indexed | 471 |
| Total index time | ~78s |
| Throughput | ~6 entities/sec |
| Pipeline | 3-stage parallel (Parse → Embed → Index) |
| S3 download threads | 16 parallel |

**Note:** Indexing is CPU-bound on embedding generation (CodeBERT). On GPU, throughput would be ~10x higher.

---

## 2. Query Latency (End-to-End)

Measured from client request to full JSON response, including network round-trip to ALB.

### Natural Language Queries

| Query | Latency | Top-1 Result | Top-1 Confidence |
|-------|---------|-------------|-----------------|
| "which code handles image extraction?" | 2.32s | `ImageExtractor` (image_extractor.py:7) | 0.90 |
| "where is the indexing pipeline?" | 2.13s | `main` (example.py:35) | 0.95 |
| "find the OpenSearch storage layer" | 1.90s | `OpenSearchStore` (opensearch_store.py:8) | 0.95 |
| "where should I add rate limiting?" | 2.75s | `search` (fault_localizer.py:62) | 0.90 |
| "which code does LLM ranking?" | 3.23s | `LLMRanker` (llm_ranker.py:7) | 0.95 |

**Average NL query latency: 2.47s**

### Stack Trace Query

| Query | Latency | Top-1 Result | Top-1 Confidence |
|-------|---------|-------------|-----------------|
| Python traceback (AttributeError in code_indexer) | 2.72s | `localize` (fault_localizer_prod.py:83) | 0.85 |

### Latency Breakdown (estimated)

| Stage | Time |
|-------|------|
| Network (client → ALB → Fargate) | ~600ms |
| CodeBERT embedding generation | ~200ms |
| OpenSearch hybrid search (BM25 + vector) | ~150ms |
| LLM re-ranking (Nova Pro) | ~1.5s |
| Response serialization | ~10ms |
| **Total** | **~2.5s** |

---

## 3. Metadata Endpoint Latency

| Endpoint | Latency |
|----------|---------|
| `GET /health` | 0.68s |
| `GET /stats` | 0.60s |
| `GET /namespaces` | 0.62s |
| `GET /namespaces/search?q=fault` | 0.61s |
| `GET /` (UI page) | 0.94s |

**Note:** ~600ms of each request is network latency (client → ALB round-trip). Actual server processing is <100ms for metadata endpoints.

---

## 4. Result Quality

### Top-1 Accuracy

| Query Type | Correct Top-1 | Total | Accuracy |
|-----------|--------------|-------|----------|
| NL queries | 4 | 5 | 80% |
| Stack trace | 1 | 1 | 100% |
| **Overall** | **5** | **6** | **83%** |

### Top-5 Relevance (NL Queries)

| Query | Relevant in Top-5 | Notes |
|-------|--------------------|-------|
| "image extraction?" | 3/5 | #1 ImageExtractor, #2 test mock, #3 UI handler |
| "indexing pipeline?" | 4/5 | S3Loader, JavaParser, IncrementalIndexer all relevant |
| "OpenSearch storage?" | 3/5 | #1 OpenSearchStore, #2-3 health/stats use it |
| "rate limiting?" | 2/5 | API entry points are reasonable suggestions |
| "LLM ranking?" | 4/5 | LLMRanker, FaultLocalizerProd, localize methods |

**Average top-5 relevance: 3.2/5 (64%)**

---

## 5. Infrastructure

| Resource | Spec | Status |
|----------|------|--------|
| App container | 1 vCPU, 6 GB RAM | Running |
| OpenSearch container | 0.5 vCPU, 1 GB RAM | Connected |
| Total entities in index | 920 | Across 2 namespaces |
| Docker image size | ~5.3 GB | Includes CodeBERT model |
| ECS deployment time | ~3 min | Image pull + health check |
| CI/CD build time | ~5-8 min | GitHub Actions |

---

## 6. Summary

| Metric | Value |
|--------|-------|
| Avg NL query latency | **2.47s** |
| Stack trace query latency | **2.72s** |
| Top-1 accuracy | **83%** |
| Indexing throughput (CPU) | **6 entities/sec** |
| Entities indexed | **471** (fault-localization codebase) |
| LLM cost per query | ~$0.003 (Nova Pro) |

The dominant latency factor is the LLM re-ranking call (~1.5s). Search itself (BM25 + vector) completes in ~150ms. Network overhead adds ~600ms per request due to ALB routing.
