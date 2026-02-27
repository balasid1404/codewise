# High-Level Design: Fault Localization System

## 1. Overview

An AI-powered fault localization system that identifies buggy code locations from stack traces or screenshots. Designed for enterprise-scale codebases (30M+ entities).

### 1.1 Problem Statement

Developers spend significant time locating the root cause of bugs. Traditional approaches require:
- Manual stack trace analysis
- Code navigation across large codebases
- Domain knowledge of system architecture

### 1.2 Solution

Automated fault localization using:
- Hybrid retrieval (BM25 + semantic embeddings)
- Call graph analysis for root cause detection
- Vision LLM for screenshot-based localization
- LLM re-ranking for accuracy

## 2. Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         Input Layer                              │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐  │
│  │ Stack Trace │  │ Screenshot  │  │ Natural Language Query  │  │
│  └─────────────┘  └─────────────┘  └─────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Extraction Layer                           │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐  │
│  │   Python    │  │    Java     │  │    Vision LLM           │  │
│  │  Extractor  │  │  Extractor  │  │  (Claude 3 Sonnet)      │  │
│  └─────────────┘  └─────────────┘  └─────────────────────────┘  │
│                              │                                  │
│                              ▼                                  │
│                    ┌─────────────────┐                          │
│                    │   UI Mapper     │                          │
│                    │ (UI → Code)     │                          │
│                    └─────────────────┘                          │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Retrieval Layer                             │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │                    OpenSearch                            │    │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────────┐  │    │
│  │  │    BM25     │  │   k-NN      │  │  Hybrid Search  │  │    │
│  │  │   Index     │  │   Index     │  │   (Combined)    │  │    │
│  │  └─────────────┘  └─────────────┘  └─────────────────┘  │    │
│  └─────────────────────────────────────────────────────────┘    │
│                              │                                   │
│                              ▼                                   │
│                    ┌─────────────────┐                          │
│                    │   Call Graph    │                          │
│                    │   Expansion     │                          │
│                    └─────────────────┘                          │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                       Ranking Layer                              │
│                    ┌─────────────────┐                          │
│                    │    LLM Ranker   │                          │
│                    │ (Claude Sonnet) │                          │
│                    └─────────────────┘                          │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                       Output Layer                               │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │  Ranked Fault Locations + Explanations                   │    │
│  │  - File path, line numbers                               │    │
│  │  - Confidence score                                      │    │
│  │  - Reason for suspicion                                  │    │
│  └─────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
```

## 3. Components

### 3.1 Extractors

| Extractor | Input | Output |
|-----------|-------|--------|
| PythonStackExtractor | Python traceback | StackFrames, exception type, message |
| JavaStackExtractor | Java stack trace | StackFrames, exception type, message |
| ImageExtractor | Screenshot (PNG/JPG) | UI elements, error text, app section |

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

### 3.2 Code Indexer

Parses source code and generates searchable entities.

**Supported Languages:** Python, Java

**Entity Types:**
- Functions/Methods
- Classes
- Modules

**Parsing:**
- Python: `ast` module
- Java: `javalang` library

**Embeddings:** CodeBERT (768 dimensions)

### 3.3 Storage (OpenSearch)

**Index Schema:**
```json
{
  "id": "entity_uuid",
  "name": "processPayment",
  "full_name": "PaymentService.processPayment",
  "entity_type": "method",
  "file_path": "app/payment/service.py",
  "start_line": 15,
  "end_line": 32,
  "signature": "def processPayment(self, amount: float)",
  "body": "...",
  "embedding": [0.1, 0.2, ...],  // 768 dims
  "calls": ["validate", "charge"]
}
```

**Indexes:**
- BM25 inverted index for text search
- HNSW vector index for semantic search

### 3.4 Call Graph

Tracks method call relationships for root cause expansion.

```
Stack trace shows: validate() failed
    ↓
Call graph: processPayment() → validate()
    ↓
Root cause likely in: processPayment()
```

### 3.5 UI Mapper

Converts UI elements to code patterns for image-based localization.

```
"Pay Now" button → ["payNow", "pay_now", "processPayment"]
"Checkout" page → ["checkout", "cart", "order"]
```

### 3.6 LLM Ranker

Re-ranks candidates using contextual understanding.

**Input:** Stack trace + top-20 candidates
**Output:** Top-5 ranked with explanations

## 4. Data Flow

### 4.1 Stack Trace Flow

```
1. Input: Stack trace text
2. Extract: Parse frames, exception type, message
3. Direct lookup: Find entities matching stack frame methods
4. Graph expansion: Find callers (potential root causes)
5. Hybrid search: BM25 + vector on query
6. Merge: Combine direct + expanded + searched candidates
7. LLM re-rank: Contextual ranking with explanations
8. Output: Top-K fault locations
```

### 4.2 Image Flow

```
1. Input: Screenshot of bug
2. Vision LLM: Extract error text, UI elements, app section
3. UI Mapper: Convert to code patterns
4. Search: Query OpenSearch with patterns
5. LLM re-rank: Rank with UI context
6. Output: Top-K fault locations
```

## 5. Scalability

### 5.1 Target Scale

| Metric | Target |
|--------|--------|
| Code entities | 30M+ |
| Repositories | 10K+ |
| Search latency | < 3s |
| Indexing throughput | 10K entities/min |

### 5.2 Scaling Strategy

**Indexing:**
- Parallel workers (16-32)
- Batch processing (500 entities/batch)
- Incremental updates

**Storage:**
- OpenSearch cluster (3+ data nodes)
- 30 shards (1M docs/shard)
- Sharding by package/repository

**Search:**
- Pre-filtering by stack trace context
- BM25 first pass (100 candidates)
- Vector search on filtered set (20 results)

### 5.3 Resource Estimates (30M entities)

| Resource | Specification |
|----------|---------------|
| OpenSearch | 3x r6g.2xlarge (64GB RAM each) |
| Storage | ~60GB |
| ECS Tasks | 2-4 Fargate tasks |
| Indexing time | ~40 min (16 workers) |

## 6. API Design

### 6.1 Endpoints

```
POST /index
  Body: { codebase_path: string, workers: int }
  Response: { indexed: int, status: string }

POST /localize
  Body: { error_text: string, top_k: int }
  Response: { results: FaultLocation[] }

POST /localize/image
  Body: { image_path: string, top_k: int }
  Response: { results: FaultLocation[] }

GET /health
  Response: { status: string }
```

### 6.2 Response Schema

```json
{
  "results": [
    {
      "name": "processPayment",
      "full_name": "PaymentService.processPayment",
      "file_path": "app/payment/service.py",
      "start_line": 15,
      "end_line": 32,
      "signature": "def processPayment(...)",
      "confidence": 0.92,
      "reason": "Handles payment processing, matches error context"
    }
  ]
}
```

## 7. Technology Stack

| Component | Technology |
|-----------|------------|
| Language | Python 3.11 |
| API Framework | FastAPI |
| Vector Store | OpenSearch 2.x |
| Embeddings | CodeBERT (sentence-transformers) |
| LLM | Amazon Bedrock (Claude 3 Sonnet) |
| Vision | Amazon Bedrock (Claude 3 Sonnet) |
| Container | Docker |
| Orchestration | ECS Fargate |
| Code Parsing | ast (Python), javalang (Java) |

## 8. Deployment

### 8.1 Infrastructure

```
┌─────────────────────────────────────────────────────────────┐
│                         AWS Cloud                            │
│  ┌─────────────────────────────────────────────────────┐    │
│  │                    VPC                               │    │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  │    │
│  │  │   ALB       │  │ ECS Fargate │  │ OpenSearch  │  │    │
│  │  │             │──│  (API)      │──│  Cluster    │  │    │
│  │  └─────────────┘  └─────────────┘  └─────────────┘  │    │
│  │                          │                           │    │
│  │                          ▼                           │    │
│  │                   ┌─────────────┐                    │    │
│  │                   │  Bedrock    │                    │    │
│  │                   │  (LLM)      │                    │    │
│  │                   └─────────────┘                    │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

### 8.2 Cost Estimate (Monthly)

| Service | Cost |
|---------|------|
| OpenSearch (3x r6g.2xlarge) | $1,500 |
| ECS Fargate (2 tasks) | $150 |
| Bedrock (1000 queries/day) | $300 |
| ALB | $50 |
| **Total** | **~$2,000** |

## 9. Performance

### 9.1 Latency Breakdown

| Stage | Latency |
|-------|---------|
| Extraction (regex) | 5ms |
| Extraction (vision LLM) | 500ms |
| BM25 search | 100ms |
| Vector search | 200ms |
| Graph expansion | 50ms |
| LLM re-ranking | 1-2s |
| **Total (stack trace)** | **~2s** |
| **Total (image)** | **~3s** |

### 9.2 Accuracy Targets

| Metric | Target |
|--------|--------|
| Top-1 accuracy | 60% |
| Top-5 accuracy | 85% |
| Top-10 accuracy | 95% |

## 10. Future Enhancements

### Phase 2
- Additional language support (Go, Rust, TypeScript)
- IDE plugin integration
- Real-time indexing via webhooks
- Caching layer (Redis)

### Phase 3
- Automated fix suggestions
- Historical bug pattern learning
- Integration with ticketing systems (Jira)
- Multi-modal input (logs + screenshots + traces)

## 11. References

- FaR-Loc paper (arXiv:2509.20552) - 3-stage fault localization pipeline
- CodeBERT - Pre-trained model for code understanding
- OpenSearch k-NN - Vector similarity search at scale
