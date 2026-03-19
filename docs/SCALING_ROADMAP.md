# CodeWise — Scaling Roadmap

Production scaling plan for CodeWise from prototype (4K entities) to enterprise scale (10B+ entities).

---

## 1. Distributed Indexing Infrastructure

### Replace Background Threading with Distributed Job Queues

Current `BackgroundIndexer` uses Python threading — won't scale beyond a single machine.

**Option A: SQS + Celery**
- SQS handles job queuing (120K messages for standard queues)
- Celery workers scale horizontally across multiple machines
- Redis as result backend (faster than SQS for status polling)
- Configure visibility timeout carefully (12-hour max) to avoid duplicate indexing jobs

**Option B: AWS Step Functions**
- Better for orchestrating the parse → resolve → embed → index pipeline
- Built-in retry logic and error handling
- Visual workflow monitoring
- Higher per-execution cost but cleaner orchestration

### Partition the Codebase

- Shard repositories by namespace or project boundaries
- Each worker processes a subset of files independently
- Use consistent hashing to distribute load evenly
- Implement partition-level progress tracking

---

## 2. Dual-Store Architecture: Neptune + OpenSearch

### Store Separation Strategy

Split data by what each store does best. No duplication of responsibilities.

**Neptune (graph structure — relationships, not content):**
- `resolved_calls` → CALLS edges (entity → entity, with weight)
- `base_classes` → INHERITS edges
- `file_imports` → IMPORTS edges
- Lightweight node metadata: id, name, file_path, entity_type, namespace

**OpenSearch (content search — bodies, embeddings):**
- `search_text` (BM25 keyword search)
- `embedding` (768-dim kNN vector search)
- `body`, `signature`, `docstring` (content fields)
- `name`, `full_name`, `file_path`, `namespace` (metadata + filtering)
- No more `resolved_calls`/`base_classes`/`file_imports` arrays — those move to Neptune

### Amazon Neptune

- Purpose-built for graph data
- Stores billions of relationships with millisecond query latency
- Native graph processing with index-free adjacency
- Supports Apache TinkerPop Gremlin for graph traversal
- Scales to 15 read replicas with automatic failover

**Graph Schema Design:**

```
Vertices:
  CodeEntity { id, name, file_path, entity_type, namespace }

Edges:
  CALLS     (source) --weight--> (target)     # resolved_calls
  INHERITS  (child)  ----------> (parent)     # base_classes
  IMPORTS   (file)   ----------> (file)       # file_imports
```

### Atomic Dual-Write via SQS Fanout

Single indexing event writes to both stores atomically using SQS fanout pattern:

```
Stage 4 (Index) — current:
  embed batch → OpenSearch bulk upsert (everything in one place)

Stage 4 (Index) — with dual-store:
  embed batch → SNS topic (single publish)
                  ├── SQS queue A → Lambda/Worker → OpenSearch bulk upsert (content + embeddings)
                  └── SQS queue B → Lambda/Worker → Neptune batch add vertices + edges (graph)
```

**How it works:**
1. After embedding, the indexer publishes a batch message to an SNS topic
2. SNS fans out to two SQS queues (one per store)
3. Each queue has a consumer that writes to its respective store
4. Both writes happen in parallel — if one fails, the message stays in its queue and retries
5. DLQ (Dead Letter Queue) on each SQS queue catches persistent failures

**Why SQS fanout over sequential writes:**
- Parallel writes — Neptune and OpenSearch update simultaneously
- Retry isolation — OpenSearch failure doesn't block Neptune (and vice versa)
- Decoupled — can scale each consumer independently
- At-least-once delivery — no data loss even if a consumer crashes
- Eventual consistency window is <1 second under normal conditions

**Consistency guarantees:**
- Both stores are eventually consistent (typically <1s lag)
- For indexing this is fine — a query hitting OpenSearch 500ms before Neptune catches up just means graph propagation uses slightly stale edges for that brief window
- DLQ monitoring alerts if either store falls behind

### Query Orchestration

The localization pipeline changes only at the graph propagation step:

```
Current localize() flow:
  1. OpenSearch hybrid search → 50 candidates
  2. GraphRanker → OpenSearch msearch on resolved_calls arrays → propagate scores
  3. CrossEncoder → 15
  4. LLM → 5

With Neptune:
  1. OpenSearch hybrid search → 50 candidates (unchanged)
  2. GraphRanker → Neptune Gremlin traversal → propagate scores
     g.V(candidate_ids).bothE('CALLS').otherV().path()
     ~5ms for 2-hop vs ~200ms for OpenSearch array scans (40x faster)
  3. CrossEncoder → 15 (unchanged)
  4. LLM → 5 (unchanged)
```

Only `GraphRanker` needs a code change — swap OpenSearch msearch for Neptune Gremlin client. Everything else in the query path stays the same.

### Dependencies endpoint (`GET /dependencies/{entity_id}`)

Currently queries OpenSearch for calls/callers/imports. With Neptune:

```
Current:  OpenSearch term queries on resolved_calls arrays
With Neptune:
  Calls:     g.V(entity_id).outE('CALLS').inV()
  Called by:  g.V(entity_id).inE('CALLS').outV()
  Inherits:  g.V(entity_id).outE('INHERITS').inV()
  Imports:   g.V(entity_id).outE('IMPORTS').inV()
```

All sub-millisecond on Neptune vs 50-100ms each on OpenSearch.

### Benefits

- 2-hop graph propagation becomes a native graph query (~5ms vs ~200ms)
- Caller/callee analysis is orders of magnitude faster
- Can add new relationship types (data flow, control flow) without schema changes
- Enables advanced queries like "find all paths between entities"
- OpenSearch index gets smaller (no graph arrays) → faster content search
- Each store scales independently

**When to migrate:** 1M+ entities. Below that, OpenSearch arrays are sufficient.

---

## 3. Embedding Model Upgrades

### Replace CodeBERT with Modern Alternatives

Current: `microsoft/codebert-base` (2020, 125M params, 768-dim)

**StarCoder Embeddings**
- Better out-of-distribution performance on diverse codebases
- Trained on The Stack (permissively licensed code)
- Handles multiple languages more effectively

**CodeT5+**
- Encoder-decoder architecture for better semantic understanding
- Supports code generation tasks if features expand later

**Hybrid Approach**
- Ensemble term-based embeddings with ID-based embeddings
- Improves ranking NDCG by ~1-2%
- Use separate models for matching vs. ranking

### Embedding Dimension Optimization

- Test 256-dim vs 768-dim trade-offs
- Larger dimensions help matching but increase storage/compute costs
- Consider quantization (INT8) for storage efficiency — 4x reduction

### Quick Win: ONNX Runtime

Before swapping models, convert existing CodeBERT to ONNX format:
- 2-4x CPU inference speedup with zero quality loss
- No model change, no reindexing needed
- Just add `onnxruntime` dependency and a backend parameter

---

## 4. Horizontal Scaling Architecture

### Cellular Architecture Pattern

Adopt cell-based partitioning for massive scale:

**Cell-based partitioning:**
- Each region has multiple cells with no cross-communication
- Reduces blast radius during failures
- Enables independent scaling per cell

**Sharding strategy:**
- Partition by repository, namespace, or file hash
- Each partition handles 5,500 GET TPS or 3,500 PUT TPS
- Automatic hot partition splitting

**Replication:**
- 5-way replication across different storage nodes
- Ensures durability without impacting query performance
- Use consistent hashing for cache distribution

### Caching Layer

**Multi-tier caching:**
- Local cache (in-memory) for frequently accessed entities
- Remote cache (Redis/ElastiCache) for cross-worker sharing
- TTL-based eviction (3-24 hours depending on data type)

**Cache warming strategies:**
- Pre-populate cache with popular repositories
- Invalidate on reindex events
- Use write-through caching for consistency

---

## 5. Optimize OpenSearch Performance

### Index Configuration

- Shard sizing: 10-30 GB per shard for search workloads
- Refresh intervals: Balance freshness vs CPU consumption
- Replica strategy: 1-2 replicas for high availability
- Index lifecycle management: Archive old namespace versions

### Query Optimization

- Use `_source` filtering to return only needed fields
- Implement query result caching for common searches
- Use bool queries with filters (cached) vs must (scored)
- Batch bulk upserts (current approach is good)

### At 10B+ Scale

OpenSearch for BM25 text search (scales well) + dedicated vector DB (Pinecone, Milvus, Qdrant) for embedding search. Query both, merge results.

---

## 6. Cost Optimization Strategies

### LLM Reranking Cost Reduction

Nova Pro reranking of 15 candidates per query will get expensive at volume.

**Semantic caching:**
- Vector similarity on queries to find cached LLM responses
- Cache LLM responses for similar error patterns
- Can reduce LLM calls by 40-60% for repeated queries

**Prompt optimization:**
- Reduce candidate descriptions to essential info only
- Use structured output format to minimize tokens
- Compress stack traces before sending to LLM

**Tiered reranking:**
- Use cross-encoder for top 50 → 15 (cheap, local)
- Only use LLM for ambiguous cases or final top-5
- Implement confidence thresholds to skip LLM when cross-encoder is confident

### Compute Optimization

- Spot instances for indexing workers (non-critical, 60-70% savings)
- Right-size Fargate tasks based on actual memory usage
- Graviton (ARM) instances for 20-40% cost savings on Python workloads
- GPU spot for batch indexing (g5.xlarge ~$0.35/hr spot vs $1/hr on-demand)

---

## 7. Monitoring and Observability

### Distributed Tracing

- Implement OpenTelemetry for end-to-end request tracing
- Track latency at each pipeline stage
- Identify bottlenecks in parse → resolve → embed → index flow

### Metrics to Track

| Metric | Target |
|---|---|
| Indexing throughput | entities/second |
| Query latency p50 | < 500ms |
| Query latency p99 | < 3s |
| Cache hit rate | > 60% |
| Graph traversal time | < 50ms |
| LLM reranking cost | $/query |

### Alerting

- Index lag (time since last commit indexed)
- Query error rates
- Resource utilization (CPU, memory, GPU)
- Cost anomalies

---

## 8. Incremental Migration Path

### Phase 1: Distributed Indexing (Weeks 1-4)
- Deploy SQS + Celery infrastructure
- Migrate indexing jobs from threading to Celery tasks
- Add partition-level progress tracking
- Test with subset of repositories

### Phase 2: Dual-Store + Graph Database (Weeks 5-8)
- Set up Neptune cluster
- Deploy SNS topic + 2 SQS queues (OpenSearch consumer + Neptune consumer)
- Implement dual-write: indexer publishes to SNS, fanout writes to both stores
- Add DLQ monitoring and retry logic per queue
- Migrate call graph data from OpenSearch arrays to Neptune edges
- Update GraphRanker to use Neptune Gremlin queries
- Update `/dependencies` endpoint to query Neptune
- Strip `resolved_calls`/`base_classes`/`file_imports` arrays from OpenSearch index
- A/B test query latency vs current approach

### Phase 3: Embedding Upgrade (Weeks 9-12)
- Evaluate StarCoder / CodeT5+ embeddings on test set
- Reindex subset with new embeddings
- Compare retrieval metrics (NDCG, Recall@100)
- Gradual rollout if metrics improve

### Phase 4: Optimization (Weeks 13-16)
- Implement caching layers (semantic cache for LLM, Redis for queries)
- Optimize OpenSearch configuration
- Add LLM cost controls and tiered reranking
- Performance tuning and monitoring dashboards

---

## Scale Estimates

| Scale | Entities | OpenSearch | Embedding Compute | Monthly Cost (est.) |
|---|---|---|---|---|
| Prototype (current) | 4K | 1 node, 2GB | 2 vCPU Fargate | ~$50 |
| Team | 100K | 1 node, 8GB | 4 vCPU Fargate + ONNX | ~$150 |
| Org | 1M | 3 nodes, 24GB | GPU spot batch | ~$500 |
| Enterprise | 100M | 20 nodes, managed | GPU cluster | ~$5K |
| Mega-scale | 10B | Dedicated vector DB + OpenSearch | 50-100 GPUs | ~$50K+ |
