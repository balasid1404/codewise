# CodeWise — Architecture

## Overview

CodeWise is an AI-powered code intelligence and fault localization system. Given a stack trace, error message, natural language question, or screenshot, it identifies the most relevant code locations in an indexed codebase.

```
┌─────────────────────────────────────────────────────────────────┐
│                         User Interface                          │
│                    (Single-page HTML/JS app)                    │
│  Localize (text/image) │ Index Codebase │ Jobs │ Namespaces    │
└────────────────────────────┬────────────────────────────────────┘
                             │ HTTP
┌────────────────────────────▼────────────────────────────────────┐
│                     FastAPI Application                          │
│                      (api.py — Fargate)                         │
│                                                                  │
│  ┌──────────┐ ┌──────────────┐ ┌───────────┐ ┌──────────────┐  │
│  │ Localize │ │ Index/Upload │ │ Webhooks  │ │ Namespaces   │  │
│  │ Endpoints│ │ Endpoints    │ │ (GitHub)  │ │ CRUD         │  │
│  └────┬─────┘ └──────┬───────┘ └───────────┘ └──────────────┘  │
│       │              │                                           │
│  ┌────▼──────────────▼──────────────────────────────────────┐   │
│  │              FaultLocalizerProd                            │   │
│  │         (fault_localizer_prod.py)                         │   │
│  │                                                            │   │
│  │  Indexing Pipeline:                                        │   │
│  │    Parse → Resolve → Embed → Index                        │   │
│  │                                                            │   │
│  │  Localization Pipeline:                                    │   │
│  │    Extract → Retrieve → Graph → CrossEncoder → LLM        │   │
│  └────────────────────────────────────────────────────────────┘   │
└──────────────────────────────┬──────────────────────────────────┘
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
     ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
     │  OpenSearch   │ │ Amazon       │ │ Redis        │
     │  (Fargate)    │ │ Bedrock      │ │ (optional)   │
     │              │ │ Nova Pro     │ │ Query cache  │
     │ - BM25 text  │ │ - LLM rank  │ └──────────────┘
     │ - kNN vector │ │ - Image OCR  │
     │ - Call graph  │ └──────────────┘
     │ - UI vocab    │
     └──────────────┘
```

## Indexing Pipeline

Converts source code into searchable entities stored in OpenSearch.

```
Source Code (.py/.java/.js/.ts/.html)
        │
        ▼
┌─────────────────────────────────────┐
│  Stage 1: PARSE (multi-threaded)    │
│                                     │
│  PythonParser   → CodeEntity[]      │
│  JavaParser     → CodeEntity[]      │
│  JsTsParser     → CodeEntity[]      │
│  HtmlParser     → CodeEntity[]      │
│                                     │
│  Each entity: id, name, signature,  │
│  body, file_path, calls, imports,   │
│  class_name, annotations            │
└──────────────┬──────────────────────┘
               ▼
┌─────────────────────────────────────┐
│  Stage 2: RESOLVE                   │
│  (RelationshipResolver)             │
│                                     │
│  1. Build name index (O(n))         │
│  2. Build file index (O(n))         │
│  3. Build module entity index       │
│  4. Build per-file import scopes    │
│  5. Resolve calls → entity IDs      │
│  6. Resolve inheritance chains      │
│  7. Resolve file-level imports      │
│                                     │
│  Output: resolved_calls,            │
│  base_classes, file_imports          │
└──────────────┬──────────────────────┘
               ▼
┌─────────────────────────────────────┐
│  Stage 3: EMBED                     │
│  (CodeBERT — microsoft/codebert-base│
│                                     │
│  - Chunk large entities (512 chars, │
│    64 overlap)                      │
│  - Skip trivial entities (<30 char) │
│  - Batch encode (256 per batch)     │
│  - Average chunk embeddings         │
│  - Output: 768-dim float vector     │
└──────────────┬──────────────────────┘
               ▼
┌─────────────────────────────────────┐
│  Stage 4: INDEX                     │
│  (OpenSearchStore — bulk upsert)    │
│                                     │
│  - Upsert by entity ID (idempotent) │
│  - Store: embedding, search_text,   │
│    calls, resolved_calls, imports,  │
│    base_classes, file_imports        │
│  - Also: ScalableUIMapper learns    │
│    word→entity vocabulary            │
└─────────────────────────────────────┘
```

Stages 3-4 run concurrently via a producer-consumer queue (embed thread → index thread).

### Input Sources

| Source | Endpoint | Flow |
|---|---|---|
| S3 URI | `POST /index` | S3CodebaseLoader downloads → parse → index |
| Zip upload | `POST /index/upload` | Extract zip → parse → index |
| Local (testing) | `POST /index/bulk-import` | Local script embeds → push pre-embedded entities |
| GitHub webhook | `POST /webhook/github` | Auto-reindex on push |

### Background Indexer

All indexing runs in background threads via `BackgroundIndexer`. Each job has:
- Stage-level progress (parsing/resolving/embedding/indexing)
- Cancel support via `threading.Event`
- Status: pending → running → completed/failed/cancelled

## Localization Pipeline

Finds the most relevant code given an error, question, or screenshot.

```
Input (stack trace / NL question / screenshot)
        │
        ▼
┌─────────────────────────────────────┐
│  Step 1: EXTRACT                    │
│                                     │
│  PythonStackExtractor — parse       │
│    Python tracebacks                │
│  JavaStackExtractor — parse Java    │
│    stack traces                     │
│  ImageExtractor — vision LLM        │
│    extracts UI elements, errors     │
│  NL detection — no frames = query   │
└──────────────┬──────────────────────┘
               ▼
┌─────────────────────────────────────┐
│  Step 2: RETRIEVE                   │
│  (OpenSearchStore.search_hybrid)    │
│                                     │
│  Stack trace mode:                  │
│  - Weighted query (top frame 3x,    │
│    first app frame 3x)             │
│  - Direct entity lookup by method   │
│    name with position-based scoring │
│  - Library frame deprioritization   │
│                                     │
│  NL/Image mode:                     │
│  - BM25 text search (top 100)      │
│  - kNN vector search on BM25 hits  │
│  - UI vocabulary pattern matching   │
│                                     │
│  Output: ~50 candidates with scores │
└──────────────┬──────────────────────┘
               ▼
┌─────────────────────────────────────┐
│  Step 3: GRAPH PROPAGATION          │
│  (GraphRanker)                      │
│                                     │
│  - If B is suspicious and A calls B │
│    → A gets a damped boost (0.4x)  │
│  - Uses resolved_calls (exact IDs)  │
│    for precise graph traversal      │
│  - Falls back to fuzzy name match   │
│  - 2-hop propagation, batch msearch │
│  - Callers boosted more than callees│
└──────────────┬──────────────────────┘
               ▼
┌─────────────────────────────────────┐
│  Step 4: CROSS-ENCODER RERANK       │
│  (CrossEncoderRanker)               │
│  Model: ms-marco-MiniLM-L-6-v2     │
│                                     │
│  - Takes (query, document) pairs    │
│  - Produces precise relevance score │
│  - 70% cross-encoder + 30% retrieval│
│  - Narrows 50 → 15 candidates      │
└──────────────┬──────────────────────┘
               ▼
┌─────────────────────────────────────┐
│  Step 5: LLM RERANK                 │
│  (LLMRanker — Amazon Nova Pro)      │
│                                     │
│  - Sends top 15 candidates + error  │
│    context to Bedrock               │
│  - LLM returns top 5 with           │
│    confidence scores + explanations │
│  - Separate prompts for stack trace │
│    vs NL query modes                │
└──────────────┬──────────────────────┘
               ▼
        Top 5 results with
        confidence + explanations
```

## Data Model

### CodeEntity (core unit)

```
CodeEntity:
  id              string     SHA-based unique ID
  name            string     Function/method/class name
  full_name       string     package.ClassName.methodName
  entity_type     enum       function | method | class
  file_path       string     Relative path in codebase
  start_line      int
  end_line        int
  signature       string     Full declaration line
  body            string     Source code (truncated to 5KB in store)
  embedding       float[768] CodeBERT vector
  namespace       string     Isolation scope (repo/project name)
  calls           string[]   Raw call names found in body
  resolved_calls  string[]   Entity IDs this calls (resolved)
  imports         string[]   Import statements in file
  base_classes    string[]   Parent classes
  file_imports    string[]   File paths this file imports from
  annotations     string[]   Decorators/annotations
  class_name      string     Enclosing class (if method)
  package         string     Package/module path
  docstring       string
```

### OpenSearch Indices

| Index | Purpose |
|---|---|
| `code_entities` | Main entity store — BM25 text + kNN vectors + graph edges |
| `ui_vocabulary` | Word → entity name mappings for UI/screenshot search |

## Infrastructure (AWS — CDK)

```
┌─────────────────────────────────────────────────┐
│                    VPC (2 AZs)                   │
│                                                   │
│  ┌─────────────────────────────────────────────┐ │
│  │              ECS Cluster                     │ │
│  │                                               │ │
│  │  ┌───────────────────┐  ┌─────────────────┐ │ │
│  │  │ App Service        │  │ OpenSearch      │ │ │
│  │  │ (Fargate)          │  │ (Fargate)       │ │ │
│  │  │                    │  │                  │ │ │
│  │  │ 2 vCPU / 6GB RAM  │  │ 1 vCPU / 2GB   │ │ │
│  │  │ FastAPI + CodeBERT │  │ OpenSearch 2.11 │ │ │
│  │  │ + CrossEncoder     │  │ 1GB heap        │ │ │
│  │  │                    │  │                  │ │ │
│  │  │ ALB (public)       │  │ Cloud Map DNS:  │ │ │
│  │  │ :8080              │  │ elasticsearch.  │ │ │
│  │  │                    │  │ faultloc.local  │ │ │
│  │  └───────────────────┘  └─────────────────┘ │ │
│  │                                               │ │
│  │  Auto-scaling: 1-2 tasks (CPU 80%)           │ │
│  └─────────────────────────────────────────────┘ │
│                                                   │
│  ┌──────────────┐  ┌──────────────┐              │
│  │ S3 Bucket    │  │ ECR Repo     │              │
│  │ Codebase     │  │ Docker image │              │
│  │ storage      │  │              │              │
│  └──────────────┘  └──────────────┘              │
└─────────────────────────────────────────────────┘

External:
  Amazon Bedrock (Nova Pro) — LLM reranking + image extraction
  GitHub Actions — CI/CD (build → ECR push → ECS force deploy)
```

## Component Map

```
fault-localization/
├── api.py                          # FastAPI app — all HTTP endpoints
├── fault_localizer_prod.py         # Core orchestrator (index + localize)
├── Dockerfile                      # Container definition
│
├── extractors/
│   ├── python_extractor.py         # Parse Python stack traces
│   ├── java_extractor.py           # Parse Java stack traces
│   ├── image_extractor.py          # Vision LLM screenshot analysis
│   └── scalable_ui_mapper.py       # UI text → code pattern mapping
│
├── indexer/
│   ├── background_indexer.py       # Job management, cancel, progress
│   ├── code_indexer.py             # Simple indexer (dev/local use)
│   ├── entities.py                 # CodeEntity dataclass
│   ├── relationship_resolver.py    # Cross-file call/import resolution
│   ├── python_parser.py            # AST-based Python parser
│   ├── java_parser.py              # Regex-based Java parser
│   ├── js_ts_parser.py             # JS/TS parser
│   ├── html_parser.py              # HTML script/handler parser
│   └── s3_loader.py                # Parallel S3 download
│
├── storage/
│   └── opensearch_store.py         # OpenSearch CRUD, search, dependencies
│
├── retrieval/
│   ├── hybrid_retriever.py         # BM25 + dense retrieval (in-memory)
│   └── smart_booster.py            # Query-aware score boosting
│
├── ranker/
│   ├── llm_ranker.py               # Bedrock Nova Pro reranking
│   ├── graph_ranker.py             # Call graph score propagation
│   ├── cross_encoder_ranker.py     # ms-marco cross-encoder reranking
│   └── solution_generator.py       # (future) fix suggestions
│
├── cache/
│   └── redis_cache.py              # Query result caching
│
├── webhooks/
│   └── git_webhook.py              # GitHub/GitLab push auto-reindex
│
├── scripts/
│   └── local_index.py              # Local embed + remote push (testing)
│
├── static/
│   └── index.html                  # Single-page UI
│
└── infra/cdk/
    ├── app.py                      # CDK app entry
    └── stack.py                    # VPC, ECS, ALB, S3, ECR
```

## Key Design Decisions

1. **Namespace isolation** — Each indexed codebase gets a namespace. Search can be scoped to one namespace or span all. Enables multi-repo indexing without conflicts.

2. **Hybrid retrieval** — BM25 keyword search (good for exact method names) + kNN vector search (good for semantic similarity). BM25 first pass narrows to 100, then vector reranks.

3. **4-stage reranking** — Retrieval → Graph propagation → Cross-encoder → LLM. Each stage is more expensive but more precise. Keeps LLM costs low by only sending 15 candidates.

4. **Resolved call graph** — `RelationshipResolver` maps raw call names to entity IDs at index time. `GraphRanker` uses these exact edges for score propagation, avoiding expensive runtime resolution.

5. **Chunk-level embeddings** — Large entities (>512 chars) are split into overlapping chunks, each embedded separately, then averaged. Preserves detail that a single embedding would lose.

6. **Self-hosted OpenSearch** — Uses OpenSearch on Fargate instead of managed AWS OpenSearch Service to avoid subscription costs during prototyping. Same API, easy to migrate later.

7. **Upsert semantics** — Re-indexing the same codebase updates existing entities by ID. No duplicates. Deleted code entities linger until namespace is manually purged.
