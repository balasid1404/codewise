# CodeWise — Code Intelligence & Fault Localization System

## Complete Implementation & Deployment Summary

---

## 1. System Overview

CodeWise is an AI-powered code intelligence system that helps developers find relevant code — whether debugging from stack traces and screenshots, or asking natural language questions like "where should I add rate limiting?" or "which code handles authentication?". It combines code embedding search (BM25 + vector similarity) with LLM-based reasoning to rank and explain results.

### Core Pipeline
```
Input (Stack Trace / NL Query / Screenshot) → Query Classification → Hybrid Retrieval (BM25 + Vector) → Dual-Mode LLM Ranking → Ranked Results with Explanations
```

### Key Capabilities
- Natural language code questions ("where should I change X?", "which code handles Y?")
- Python and Java stack trace parsing for fault localization
- JavaScript, TypeScript, and HTML code parsing via tree-sitter AST
- Hybrid search: BM25 text search + CodeBERT vector embeddings (768-dim)
- Dual-mode LLM ranking: relevance-based for NL queries, fault-based for stack traces (Amazon Nova Pro via Bedrock)
- Screenshot-based localization via Vision LLM
- Multi-org namespace isolation with auto-detection
- 3-stage parallel indexing pipeline (Parse → Embed → Index)
- Background codebase indexing from S3
- Incremental indexing (only changed files)
- Call graph analysis for related method discovery
- Web UI for interactive use
- REST API for programmatic access
- GitHub webhook for auto-reindexing on push

---

## 2. Project Structure

```
fault-localization/
├── api.py                          # FastAPI application (main entry point)
├── fault_localizer.py              # Core fault localizer (local/dev)
├── fault_localizer_prod.py         # Production fault localizer
├── cli.py                          # CLI interface
├── example.py                      # Usage examples
├── Dockerfile                      # Container image definition
├── docker-compose.yml              # Local dev compose
├── requirements.txt                # Python dependencies
├── .dockerignore                   # Docker build exclusions
├── .gitignore                      # Git exclusions
├── pytest.ini                      # Test configuration
│
├── static/
│   └── index.html                  # Web UI (single-page app)
│
├── extractors/                     # Error/stack trace parsers
│   ├── base.py                     # Base extractor + data models
│   ├── python_extractor.py         # Python traceback parser
│   ├── java_extractor.py           # Java stack trace parser
│   ├── image_extractor.py          # Screenshot-based error extraction
│   ├── learned_ui_mapper.py        # ML-based UI element mapper
│   └── scalable_ui_mapper.py       # Scalable UI vocabulary mapper
│
├── indexer/                        # Codebase indexing pipeline
│   ├── entities.py                 # CodeEntity data model
│   ├── code_indexer.py             # Main indexer orchestrator
│   ├── python_parser.py            # Python AST parser
│   ├── java_parser.py              # Java AST parser
│   ├── js_ts_parser.py             # JavaScript/TypeScript tree-sitter parser
│   ├── html_parser.py              # HTML tree-sitter parser (+ inline JS)
│   ├── background_indexer.py       # Async job management
│   ├── incremental_indexer.py      # Hash-based change detection
│   ├── multi_repo_indexer.py       # Multi-repository support
│   ├── s3_loader.py                # S3 codebase downloader (.py/.java/.js/.ts/.html)
│   └── local_cache.py             # Local file caching
│
├── storage/                        # Search backends
│   ├── base.py                     # Abstract VectorStore interface
│   └── opensearch_store.py         # OpenSearch/Elasticsearch implementation
│
├── retrieval/                      # Search & retrieval
│   ├── hybrid_retriever.py         # BM25 + vector hybrid search
│   └── smart_booster.py            # Call-graph-aware score boosting
│
├── ranker/                         # LLM-based ranking
│   ├── llm_ranker.py               # Amazon Nova Pro ranking via Bedrock
│   ├── confidence_calibrator.py    # Confidence score calibration
│   └── solution_generator.py       # Fix suggestion generator
│
├── graph/                          # Code analysis
│   └── call_graph.py               # Static call graph builder
│
├── cache/                          # Response caching
│   └── redis_cache.py              # Redis-based query cache
│
├── feedback/                       # User feedback loop
│   └── feedback_store.py           # Feedback storage
│
├── webhooks/                       # Integrations
│   └── git_webhook.py              # GitHub push webhook handler
│
├── utils/
│   └── retry.py                    # Retry utilities
│
├── tests/                          # Test suite
│   ├── test_extractors.py
│   ├── test_graph.py
│   ├── test_image_localization.py
│   └── test_integration.py
│
├── scripts/                        # Dev/benchmark scripts
│   ├── benchmark.py
│   ├── generate_test_codebase.py
│   ├── scale_estimate.py
│   ├── test_e2e_image.py
│   ├── test_image_mock.py
│   └── test_learned_mapper.py
│
├── infra/
│   └── cdk/                        # AWS CDK infrastructure
│       ├── app.py                  # CDK app entry point
│       ├── stack.py                # Prototype stack (current)
│       ├── stack_production.py     # Production stack (saved)
│       ├── cdk.json                # CDK config
│       └── requirements.txt        # CDK Python deps
│
├── .github/
│   └── workflows/
│       └── deploy.yml              # CI/CD pipeline
│
└── docs/
    ├── HLD.md                      # High-level design
    └── DEPLOYMENT_SUMMARY.md       # This file
```

---

## 3. API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Web UI |
| `GET` | `/health` | Health check (OpenSearch, cache status) |
| `GET` | `/stats` | System statistics (indexed entities count) |
| `POST` | `/index` | Start background indexing job |
| `GET` | `/index/{job_id}` | Get indexing job status |
| `GET` | `/index/jobs/list` | List all indexing jobs |
| `POST` | `/localize` | Localize from text — stack trace or NL question (namespace auto-detected) |
| `POST` | `/localize/unified` | Unified: text + optional image (namespace auto-detected) |
| `POST` | `/localize/image` | Localize from screenshot (legacy) |
| `POST` | `/localize/image/upload` | Localize from uploaded screenshot (legacy) |
| `GET` | `/namespaces` | List all indexed namespaces |
| `GET` | `/namespaces/search?q=` | Search namespaces (typeahead autocomplete) |
| `POST` | `/webhook/github` | GitHub push webhook for auto-reindex |

### Example: Localize a Fault
```bash
curl -X POST http://<ALB_URL>/localize \
  -H "Content-Type: application/json" \
  -d '{
    "error_text": "Traceback (most recent call last):\n  File \"app.py\" ...\nTypeError: ...",
    "top_k": 5
  }'
```

### Example: Index a Codebase from S3
```bash
curl -X POST http://<ALB_URL>/index \
  -H "Content-Type: application/json" \
  -d '{"s3_uri": "s3://fault-loc-codebase-650251724071/my-project/", "workers": 2}'
```

---

## 4. Technology Stack

| Component | Technology | Purpose |
|-----------|-----------|---------|
| API Framework | FastAPI + Uvicorn | REST API server |
| Search Engine | OpenSearch 2.11 (self-hosted on Fargate) | BM25 + vector search |
| Embeddings | CodeBERT (`microsoft/codebert-base`) via sentence-transformers | 768-dim code embeddings |
| LLM | Amazon Nova Pro (`amazon.nova-pro-v1:0`) via Bedrock Converse API | Ranking, explanation & image analysis |
| Container Runtime | AWS Fargate | Serverless containers |
| Load Balancer | Application Load Balancer (ALB) | HTTP routing + health checks |
| Container Registry | Amazon ECR | Docker image storage |
| Object Storage | Amazon S3 | Codebase storage |
| Service Discovery | AWS Cloud Map | Internal DNS (`elasticsearch.faultloc.local`) |
| Infrastructure | AWS CDK (Python) | Infrastructure as Code |
| CI/CD | GitHub Actions | Auto build + deploy on push |
| Code Parsing | Python AST + javalang + tree-sitter (JS/TS/HTML) | Extract functions/classes/methods |

---

## 5. AWS Infrastructure (Prototype)

### Account & Region
- AWS Account: `650251724071`
- Region: `us-east-1`
- IAM User: `ecom-pipeline-user`
- CloudFormation Stack: `FaultLocalizationStack`

### Resources Deployed

| Resource | Specification | Details |
|----------|--------------|---------|
| VPC | 2 AZs, public subnets only, no NAT | Cost-optimized for prototype |
| ECS Cluster | Fargate | `FaultLocCluster` with Cloud Map namespace `faultloc.local` |
| App Service | 1 vCPU, 6 GB RAM, 1 task | Runs the FastAPI app from ECR image |
| OpenSearch Service | 0.5 vCPU, 1 GB RAM, 1 task | Self-hosted OpenSearch 2.11 container |
| ALB | Public, internet-facing | Routes to app on port 8080 |
| S3 Bucket | `fault-loc-codebase-650251724071` | Versioned, S3-managed encryption |
| ECR Repository | `fault-localization` | Stores Docker images |
| Auto-scaling | 1–2 tasks, 80% CPU target | App service only |
| CloudWatch Logs | 1-week retention | Both app and OpenSearch logs |

### Live Endpoints
- API/UI: `http://FaultL-Fault-XibxofP5guVC-1959899228.us-east-1.elb.amazonaws.com`
- ECR: `650251724071.dkr.ecr.us-east-1.amazonaws.com/fault-localization`
- S3: `s3://fault-loc-codebase-650251724071/`
- OpenSearch (internal): `elasticsearch.faultloc.local:9200`

### IAM Permissions (Task Role)
- `s3:GetObject` on codebase bucket
- `bedrock:InvokeModel` (for Nova Pro LLM calls)

---

## 6. CI/CD Pipeline

### GitHub Repository
- URL: `https://github.com/balasid1404/codewise.git`
- Branch: `main`

### Workflow: `.github/workflows/deploy.yml`
Triggers on every push to `main`:

1. Checkout code
2. Configure AWS credentials (from GitHub Secrets)
3. Login to ECR
4. Build Docker image (includes ML model pre-download)
5. Tag with commit SHA + `latest`
6. Push to ECR
7. Force new ECS deployment

### GitHub Secrets Required
- `AWS_ACCESS_KEY_ID` — IAM access key
- `AWS_SECRET_ACCESS_KEY` — IAM secret key

### Build Time
- ~5–8 minutes (model download adds ~2 min to Docker build)
- ECS rollout adds ~2–3 minutes after push

---

## 7. Docker Image

```dockerfile
FROM python:3.11-slim
# Install deps, pre-download CodeBERT model at build time
# Runs: uvicorn api:app --host 0.0.0.0 --port 8080
```

- Base: `python:3.11-slim`
- Image size: ~5.3 GB (includes CodeBERT model weights)
- Port: 8080
- Health check: `GET /health`

---

## 8. Web UI

Single-page dark-themed interface served at `/` with three tabs:

1. **Localize** — Unified code search: paste a stack trace, ask a natural language question ("where should I add rate limiting?"), or describe what you're looking for. Optionally attach a screenshot. The system auto-detects whether input is a stack trace or NL query and uses the appropriate LLM prompt (fault localization vs relevance ranking). Namespace auto-detected or searchable via typeahead.
2. **Index Codebase** — Enter S3 URI and namespace to start indexing with live progress polling
3. **Jobs** — View all indexing jobs and their status

Status bar shows real-time health and OpenSearch connection status.

---

## 9. Current State

| Component | Status |
|-----------|--------|
| Infrastructure (CDK) | ✅ Deployed |
| App container | ✅ Running (6 GB, ECR image) |
| OpenSearch container | ✅ Running and connected |
| Codebase indexed | ✅ 471 entities from fault-localization codebase |
| Fault localization (stack traces) | ✅ Working end-to-end |
| NL code queries | ✅ Dual-mode LLM prompts (relevance vs fault ranking) |
| LLM ranking | ✅ Amazon Nova Pro via Bedrock |
| Namespace isolation | ✅ Auto-detect + searchable typeahead |
| CI/CD pipeline | ✅ GitHub Actions auto-deploy |
| Web UI | ✅ Deployed |
| Image localization | ✅ Amazon Nova Pro via Bedrock Converse API |
| JS/TS/HTML parsing | ✅ tree-sitter AST (native packages) |
| 3-stage parallel indexing | ✅ Parse → Embed → Index via bounded queues |
| Redis cache | ❌ Not deployed (optional, runs degraded) |
| GitHub webhook | ⚠️ Configured in code, needs webhook URL setup |

---

## 10. Production Stack (Saved)

A production-grade stack is saved at `infra/cdk/stack_production.py` with:
- Managed AWS OpenSearch Service (requires subscription activation)
- `t3.small.search` instance, 20 GB EBS GP3
- VPC with private isolated subnets
- Encryption at rest + node-to-node encryption
- HTTPS enforced

To switch to production: activate OpenSearch in the AWS console, then swap `stack.py` with `stack_production.py`.

---

## 11. Key Decisions & Workarounds

| Decision | Reason |
|----------|--------|
| Self-hosted OpenSearch instead of managed | AWS account lacks OpenSearch subscription; avoids `SubscriptionRequiredException` |
| `nginx:alpine` placeholder → ECR image | No Docker on dev machine; GitHub Actions builds images |
| CodeBERT pre-downloaded in Docker build | Prevents OOM/timeout during Fargate startup |
| 6 GB memory for app task | CodeBERT model (~400 MB) + embedding generation during indexing needs headroom |
| Amazon Nova Pro instead of Claude | Anthropic models require use case form submission; Nova Pro works immediately for both text ranking and image analysis |
| Unified localize tab | Merged text and image localization into single tab; text + optional image combined for stronger signal |
| Dual-mode LLM prompts | NL queries get a relevance-ranking prompt ("which code is most relevant?"); stack traces get a fault-localization prompt ("find the root cause"). Same ranker, different framing |
| Raw query text for NL search | Stack traces build structured queries (`exception + message + methods`); NL queries use the raw text directly for better semantic matching |
| Bedrock Converse API instead of InvokeModel | Model-agnostic API, works across all Bedrock providers |
| Cloud Map service discovery | Allows app container to reach OpenSearch via DNS (`elasticsearch.faultloc.local`) |
| Public subnets only, no NAT | Cost savings for prototype; NAT gateway costs ~$32/month |
| Native tree-sitter packages over tree-sitter-languages | `tree-sitter-languages` is abandoned and incompatible with tree-sitter ≥0.22; using `tree-sitter-javascript`, `tree-sitter-typescript`, `tree-sitter-html` directly |
| S3 loader downloads all supported file types | `.py`, `.java`, `.js`, `.ts`, `.html` — originally only downloaded Python and Java |
| Namespace-scoped search | 30M+ entities across orgs; unscoped search returns irrelevant results |
| Searchable typeahead over dropdown | Can't put millions of namespaces in a dropdown |
| 3-stage parallel indexing pipeline | Parse → Embed → Index run concurrently via bounded queues; maximizes throughput |
| 16 parallel S3 download threads | Each thread gets its own boto3 client for thread safety; saturates network bandwidth |
| Deduplicate by name + signature + line | Prevents duplicate results when same entity matches multiple search strategies |
