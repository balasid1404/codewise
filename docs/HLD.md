# High-Level Design: CodeWise — Fault Localization System

## 1. Overview

An AI-powered fault localization system that identifies buggy code locations from stack traces, screenshots, or natural language queries. Designed for enterprise-scale codebases (30M+ entities, 10K+ repositories) with multi-org namespace isolation.

### 1.1 Problem Statement

Developers spend significant time locating the root cause of bugs. Traditional approaches require:
- Manual stack trace analysis
- Code navigation across large codebases
- Domain knowledge of system architecture
- Knowing which team/org owns the relevant code

### 1.2 Solution

Automated fault localization using:
- Namespace-scoped search (auto-detected or user-selected)
- Hybrid retrieval (BM25 + semantic embeddings)
- Call graph analysis for root cause detection
- Vision LLM for screenshot-based localization (Amazon Nova Pro)
- LLM re-ranking for accuracy
- Multi-language support (Python, Java, JavaScript, TypeScript, HTML)

---

## 2. Architecture

### 2.1 Current Prototype

```
User → ALB → Single Fargate Task (1 vCPU, 6GB) → Self-hosted OpenSearch (Fargate)
                                                 → S3 (codebase storage)
                                                 → Bedrock Nova Pro (ranking + image)
```

### 2.2 Enterprise Scale Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                          Input Layer                                  │
│   Stack Trace  │  Screenshot (PNG/JPG)  │  Natural Language Query     │
└────────────────────────────┬─────────────────────────────────────────┘
                             │
┌────────────────────────────▼─────────────────────────────────────────┐
│                    API Gateway + ALB                                   │
│              Rate limiting, auth, routing                              │
└────────────────────────────┬─────────────────────────────────────────┘
                             │
┌────────────────────────────▼─────────────────────────────────────────┐
│                 Query Service (Fargate, 4–8 tasks)                    │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  1. Namespace Resolution                                        │  │
│  │     • Auto-detect from stack trace file paths / package names   │  │
│  │     • Auto-detect from image (LLM identifies app/org)           │  │
│  │     • Fallback: searchable typeahead (user picks)               │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  2. Extraction                                                  │  │
│  │     • Python/Java stack trace parsing                           │  │
│  │     • Vision LLM extraction (Nova Pro)                          │  │
│  │     • UI element → code pattern mapping                         │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  3. Scoped Hybrid Search                                        │  │
│  │     • BM25 + vector search filtered by namespace                │  │
│  │     • Call graph expansion within namespace                     │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  4. LLM Re-ranking (Nova Pro via Bedrock)                       │  │
│  │     • Contextual ranking with explanations                      │  │
│  └─────────────────────────────────────────────────────────────────┘  │
└───────────┬──────────────────────────────┬───────────────────────────┘
            │                              │
┌───────────▼───────────┐    ┌────────────▼────────────────────────────┐
│      Bedrock           │    │     OpenSearch Cluster                   │
│   (Nova Pro v1:0)      │    │     3x r6g.2xlarge (64GB each)          │
│   • LLM ranking        │    │                                         │
│   • Image extraction   │    │  ┌───────────────────────────────────┐  │
│   • Namespace inference │    │  │  Index: code_entities              │  │
└────────────────────────┘    │  │  • namespace (keyword, routed)     │  │
                              │  │  • BM25 inverted index             │  │
                              │  │  • HNSW vector index (768-dim)     │  │
                              │  │  • 30 shards, routing by namespace │  │
                              │  └───────────────────────────────────┘  │
                              └─────────────────────────────────────────┘
                                               ▲
                                               │
┌──────────────────────────────────────────────┴───────────────────────┐
│                   Indexing Pipeline (async, event-driven)              │
│                                                                       │
│  ┌──────────────┐    ┌────────────────┐    ┌───────────────────────┐  │
│  │  SQS Queue    │───▶│  Index Workers  │───▶│  Embedding Gen       │  │
│  │  (per-repo    │    │  (Fargate)      │    │  (GPU or Bedrock     │  │
│  │   changes)    │    │  16–32 tasks    │    │   Titan Embeddings)  │  │
│  └──────▲───────┘    └────────────────┘    └───────────────────────┘  │
│         │                                                             │
│  ┌──────┴───────────────────────────────────────────────────────────┐ │
│  │  Triggers                                                        │ │
│  │  • Git webhooks (push → incremental reindex changed files)       │ │
│  │  • Manual via API/UI (full reindex)                              │ │
│  │  • CodePipeline integration                                      │ │
│  └──────────────────────────────────────────────────────────────────┘ │
└───────────────────────────────────────────────────────────────────────┘
```

---

## 3. Namespace System

### 3.1 Concept

At enterprise scale (30M+ entities across 10K+ repos), search must be scoped. A bug in Amazon Music shouldn't return results from Prime Video's codebase.

Every indexed entity gets a `namespace` field. Queries are filtered by namespace before search.

### 3.2 Namespace Resolution (Priority Order)

1. **Auto-detect from stack trace** — file paths and package names reveal the source:
   - `com.amazon.music.player.PlaybackService` → namespace: `music`
   - `File "/src/music-backend/api/routes.py"` → namespace: `music-backend`
   - Match against known indexed namespaces using prefix/fuzzy matching

2. **Auto-detect from image** — the Vision LLM (Nova Pro) identifies the app:
   - Screenshot shows "Amazon Music" branding → namespace: `music`
   - Added to the image extraction prompt

3. **Searchable typeahead** — fallback when auto-detection fails:
   - User types "mus..." → autocomplete shows `music`, `music-backend`, `music-player`
   - Fetches top 10 matching namespaces from OpenSearch aggregation
   - No dropdown with millions of entries — just search-as-you-type

4. **Explicit override** — user can always manually specify a namespace

### 3.3 Namespace Derivation During Indexing

Namespaces are auto-derived from the source, not hardcoded:

| Source | Namespace Derivation |
|--------|---------------------|
| S3 URI `s3://bucket/music-backend/` | `music-backend` (first path segment after bucket) |
| Git repo `github.com/org/music-backend` | `music-backend` (repo name) |
| Manual API parameter | Whatever the user provides |

### 3.4 OpenSearch Schema

```json
{
  "namespace": { "type": "keyword" },
  "id": { "type": "keyword" },
  "name": { "type": "keyword" },
  "full_name": { "type": "keyword" },
  "entity_type": { "type": "keyword" },
  "file_path": { "type": "keyword" },
  "signature": { "type": "text" },
  "body": { "type": "text" },
  "embedding": { "type": "knn_vector", "dimension": 768 },
  "calls": { "type": "keyword" }
}
```

All search queries include a `term` filter on `namespace` for scoped results.

---

## 4. Components

### 4.1 Extractors

| Extractor | Input | Output |
|-----------|-------|--------|
| PythonStackExtractor | Python traceback | StackFrames, exception type, message |
| JavaStackExtractor | Java stack trace | StackFrames, exception type, message |
| ImageExtractor | Screenshot (PNG/JPG) | UI elements, error text, app section, namespace hint |

**Stack Frame Structure:**
```python
@dataclass
class StackFrame:
    file_path: str
    line_number: int
    method_name: str
    class_name: Optional[str]
    package: Optional[str]
```

### 4.2 Code Indexer

Parses source code and generates searchable entities.

**Supported Languages:**

| Language | Parser | Entities Extracted |
|----------|--------|--------------------|
| Python | `ast` module | Functions, classes, methods |
| Java | `javalang` | Classes, methods, constructors |
| JavaScript | `tree-sitter-javascript` | Functions, arrow functions, classes, methods |
| TypeScript | `tree-sitter-typescript` | Functions, arrow functions, classes, methods |
| HTML | `tree-sitter-html` | Page entities, inline JS functions |

**Embeddings:** CodeBERT (`microsoft/codebert-base`, 768 dimensions)

### 4.3 Storage (OpenSearch)

**Prototype:** Self-hosted OpenSearch 2.11 container on Fargate
**Production:** Managed AWS OpenSearch Service, 3x r6g.2xlarge

**Indexes:**
- BM25 inverted index for text search
- HNSW vector index for semantic search (nmslib engine, cosine similarity)
- Namespace keyword field for scoped filtering

### 4.4 Call Graph

Tracks method call relationships for root cause expansion.

```
Stack trace shows: validate() failed
    ↓
Call graph: processPayment() → validate()
    ↓
Root cause likely in: processPayment()
```

### 4.5 UI Mapper

Converts UI elements to code patterns for image-based localization.

```
"Pay Now" button → ["payNow", "pay_now", "processPayment"]
"Checkout" page → ["checkout", "cart", "order"]
```

### 4.6 LLM Ranker

Re-ranks candidates using contextual understanding via Amazon Nova Pro (Bedrock Converse API).

**Input:** Error context + top-20 candidates
**Output:** Top-5 ranked with explanations and confidence scores

---

## 5. Data Flow

### 5.1 Stack Trace Flow

```
1. Input: Stack trace text
2. Namespace: Auto-detect from file paths / package names
3. Extract: Parse frames, exception type, message
4. Direct lookup: Find entities matching stack frame methods (scoped by namespace)
5. Graph expansion: Find callers (potential root causes)
6. Hybrid search: BM25 + vector on query (scoped by namespace)
7. Merge + deduplicate candidates
8. LLM re-rank: Contextual ranking with explanations
9. Output: Top-K fault locations
```

### 5.2 Image Flow

```
1. Input: Screenshot of bug
2. Vision LLM: Extract error text, UI elements, app section, namespace hint
3. Namespace: Use LLM-detected app name, or fallback to typeahead
4. UI Mapper: Convert UI elements to code patterns
5. Search: Query OpenSearch with patterns (scoped by namespace)
6. LLM re-rank: Rank with UI context
7. Output: Top-K fault locations
```

### 5.3 Unified Flow (Text + Image)

```
1. Input: Text (stack trace / query) + optional screenshot
2. Namespace: Auto-detect from text paths, or image app name, or typeahead
3. Extract both: Parse text + Vision LLM on image
4. Combined search: Merge candidates from text search + image search
5. Deduplicate by entity identity (name + signature + line)
6. LLM re-rank with combined context
7. Output: Top-K fault locations + image extraction JSON
```

---

## 6. Scalability

### 6.1 Target Scale

| Metric | Target |
|--------|--------|
| Code entities | 30M+ |
| Repositories | 10K+ |
| Namespaces (orgs/teams) | 1000+ |
| Search latency | < 3s |
| Indexing throughput | 10K entities/min (CPU), 60K/min (GPU) |

### 6.2 Scaling Strategy

**Indexing:**
- Event-driven via SQS (git push → queue → worker)
- 16–32 parallel Fargate workers
- GPU instances for embedding generation (p3.2xlarge or g5.xlarge)
- Alternative: Bedrock Titan Embeddings API (no GPU infra needed)
- Incremental only after initial full index (hash-based change detection)
- Batch processing (500 entities/batch to OpenSearch)

**Storage:**
- Managed AWS OpenSearch cluster (3+ data nodes, r6g.2xlarge)
- 30 shards with namespace-based routing
- ~60GB storage for 30M entities
- Read replicas for search-heavy workloads

**Search:**
- Namespace filter applied before any search (eliminates 99%+ of index)
- BM25 first pass (100 candidates from scoped index)
- Vector search on filtered set (20 results)
- Call graph expansion within namespace only

**Query Service:**
- 4–8 Fargate tasks behind ALB
- Auto-scaling on CPU (target 80%)
- Stateless — all state in OpenSearch

### 6.3 Indexing Time Estimates

| Scale | CPU (4 workers) | CPU (16 workers) | GPU (4 workers) |
|-------|-----------------|-------------------|-----------------|
| 500 entities | ~30s | ~10s | ~5s |
| 30K entities | ~30min | ~8min | ~2min |
| 1M entities | ~16hr | ~4hr | ~30min |
| 30M entities | ~21hr | ~5hr | ~2hr |

After initial index, incremental updates (per git push) take seconds.

### 6.4 Resource Estimates (30M entities, production)

| Resource | Specification | Monthly Cost |
|----------|--------------|-------------|
| OpenSearch | 3x r6g.2xlarge (64GB RAM each) | ~$1,500 |
| Query Service | 4x Fargate tasks (1 vCPU, 4GB) | ~$300 |
| Indexing Workers | On-demand Fargate (burst) | ~$100 |
| Bedrock Nova Pro | ~5K queries/day | ~$500 |
| SQS + S3 + CloudWatch | Misc | ~$50 |
| **Total** | | **~$2,500/month** |

---

## 7. API Design

### 7.1 Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Web UI |
| `GET` | `/health` | Health check |
| `GET` | `/stats` | System statistics |
| `POST` | `/index` | Start indexing job (namespace auto-derived from S3 path) |
| `GET` | `/index/{job_id}` | Job status |
| `GET` | `/index/jobs/list` | List all jobs |
| `POST` | `/localize` | Localize from text (namespace auto-detected) |
| `POST` | `/localize/unified` | Text + optional image (namespace auto-detected) |
| `POST` | `/localize/image/upload` | Image upload (namespace from LLM or typeahead) |
| `GET` | `/namespaces` | List indexed namespaces (for typeahead) |
| `GET` | `/namespaces/search?q=mus` | Search namespaces (typeahead autocomplete) |
| `POST` | `/webhook/github` | Git push webhook for auto-reindex |

### 7.2 Response Schema

```json
{
  "results": [
    {
      "name": "processPayment",
      "full_name": "PaymentService.processPayment",
      "file_path": "app/payment/service.py",
      "start_line": 15,
      "end_line": 32,
      "signature": "def processPayment(self, amount: float)",
      "confidence": 0.92,
      "confidence_label": "high",
      "reason": "Handles payment processing, matches error context",
      "namespace": "music-backend"
    }
  ],
  "namespace_used": "music-backend",
  "namespace_source": "auto-detected"
}
```

---

## 8. Technology Stack

| Component | Technology | Purpose |
|-----------|-----------|---------|
| Language | Python 3.11 | Application code |
| API Framework | FastAPI + Uvicorn | REST API server |
| Search Engine | OpenSearch 2.11+ | BM25 + vector search |
| Embeddings | CodeBERT (`microsoft/codebert-base`) | 768-dim code embeddings |
| LLM | Amazon Nova Pro (`amazon.nova-pro-v1:0`) | Ranking, explanation, image analysis |
| LLM API | Bedrock Converse API | Model-agnostic interface |
| Container Runtime | AWS Fargate | Serverless containers |
| Load Balancer | ALB | HTTP routing + health checks |
| Container Registry | Amazon ECR | Docker image storage |
| Object Storage | Amazon S3 | Codebase storage |
| Service Discovery | AWS Cloud Map | Internal DNS |
| Queue | Amazon SQS | Async indexing triggers |
| Infrastructure | AWS CDK (Python) | Infrastructure as Code |
| CI/CD | GitHub Actions | Auto build + deploy on push |
| Python Parsing | `ast` module | Python AST extraction |
| Java Parsing | `javalang` | Java AST extraction |
| JS/TS Parsing | `tree-sitter` + `tree-sitter-javascript/typescript` | JS/TS AST extraction |
| HTML Parsing | `tree-sitter` + `tree-sitter-html` | HTML + inline JS extraction |

---

## 9. Deployment

### 9.1 Prototype (Current)

| Resource | Spec |
|----------|------|
| VPC | 2 AZs, public subnets only, no NAT |
| App Service | 1 vCPU, 6GB RAM, Fargate |
| OpenSearch | Self-hosted container (0.5 vCPU, 1GB) |
| ALB | Public, internet-facing |
| S3 | `fault-loc-codebase-650251724071` |
| ECR | `fault-localization` |
| Region | us-east-1 |

### 9.2 Production

| Resource | Spec |
|----------|------|
| VPC | Multi-AZ, private subnets, NAT gateway |
| Query Service | 4–8 Fargate tasks, auto-scaling |
| OpenSearch | Managed AWS OpenSearch, 3x r6g.2xlarge |
| Indexing | On-demand Fargate workers + SQS |
| ALB | With WAF, HTTPS enforced |
| Monitoring | CloudWatch dashboards, alarms on latency/errors |

### 9.3 Infrastructure Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                           AWS Cloud                                  │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │                          VPC                                   │  │
│  │                                                                │  │
│  │  ┌─────────┐    ┌──────────────┐    ┌───────────────────────┐  │  │
│  │  │   ALB   │───▶│ Query Service │───▶│  OpenSearch Cluster   │  │  │
│  │  │  (HTTPS)│    │ (Fargate x4)  │    │  (3x r6g.2xlarge)    │  │  │
│  │  └─────────┘    └──────┬───────┘    └───────────────────────┘  │  │
│  │                        │                        ▲               │  │
│  │                        ▼                        │               │  │
│  │                 ┌─────────────┐    ┌────────────┴────────────┐  │  │
│  │                 │   Bedrock   │    │   Index Workers          │  │  │
│  │                 │  (Nova Pro) │    │   (Fargate, on-demand)  │  │  │
│  │                 └─────────────┘    └────────────▲────────────┘  │  │
│  │                                                 │               │  │
│  │                                    ┌────────────┴────────────┐  │  │
│  │                                    │   SQS (index queue)     │  │  │
│  │                                    └────────────▲────────────┘  │  │
│  └─────────────────────────────────────────────────┼──────────────┘  │
│                                                    │                 │
│  ┌──────────┐    ┌──────────┐    ┌────────────────┴──────────────┐  │
│  │   ECR    │    │    S3    │    │  Git Webhooks / CodePipeline  │  │
│  └──────────┘    └──────────┘    └───────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 10. Performance

### 10.1 Latency Breakdown

| Stage | Latency |
|-------|---------|
| Namespace resolution | 5–50ms |
| Extraction (regex) | 5ms |
| Extraction (Vision LLM) | 500ms |
| BM25 search (scoped) | 50–100ms |
| Vector search (scoped) | 100–200ms |
| Graph expansion | 50ms |
| LLM re-ranking | 1–2s |
| **Total (stack trace)** | **~2s** |
| **Total (image)** | **~3s** |
| **Total (unified)** | **~3.5s** |

### 10.2 Accuracy Targets

| Metric | Target |
|--------|--------|
| Top-1 accuracy | 60% |
| Top-5 accuracy | 85% |
| Top-10 accuracy | 95% |
| Namespace auto-detection | 90%+ (from stack traces) |

---

## 11. Key Decisions

| Decision | Reason |
|----------|--------|
| Namespace-scoped search | 30M+ entities across orgs; unscoped search returns irrelevant results and is slow |
| Auto-detect namespace from input | Eliminates manual selection for 90%+ of queries |
| Searchable typeahead over dropdown | Can't put millions of namespaces in a dropdown |
| Native tree-sitter over tree-sitter-languages | `tree-sitter-languages` is abandoned, incompatible with tree-sitter ≥0.22 |
| Amazon Nova Pro over Claude | Anthropic models require use case form; Nova Pro works immediately |
| Bedrock Converse API | Model-agnostic, works across all Bedrock providers |
| Self-hosted OpenSearch (prototype) | AWS account lacks managed OpenSearch subscription |
| SQS for indexing triggers | Decouples git events from indexing; handles burst; retry built-in |
| Incremental indexing | Full reindex of 30M entities takes hours; incremental takes seconds |
| CodeBERT embeddings | Best code-specific embedding model; 768-dim balances quality vs storage |

---

## 12. Future Enhancements

### Phase 2
- Additional language support (Go, Rust, C/C++)
- IDE plugin integration (VS Code, IntelliJ)
- Redis caching layer for frequent queries
- Namespace hierarchy (org → team → repo)

### Phase 3
- Automated fix suggestions with code diffs
- Historical bug pattern learning (feedback loop)
- Integration with ticketing systems (Jira, ServiceNow)
- Multi-modal input (logs + screenshots + traces combined)
- Cross-namespace search for shared libraries

---

## 13. References

- CodeBERT — Pre-trained model for code understanding
- OpenSearch k-NN — Vector similarity search at scale
- tree-sitter — Incremental parsing for JS/TS/HTML
- Amazon Bedrock Converse API — Unified LLM interface
