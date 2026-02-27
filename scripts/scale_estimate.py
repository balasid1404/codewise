#!/usr/bin/env python3
"""Estimate resource requirements for 30M entities."""

# Based on benchmark: 11K entities in 14s, 203KB memory

BENCHMARK_ENTITIES = 11000
BENCHMARK_INDEX_TIME = 14.37  # seconds
BENCHMARK_SEARCH_TIME = 1.5   # seconds average
BENCHMARK_MEMORY_KB = 203

TARGET_ENTITIES = 30_000_000

def estimate():
    print("=" * 60)
    print("SCALE ESTIMATION: 30M Entities")
    print("=" * 60)

    # Indexing
    print("\n📊 INDEXING")
    single_thread_hours = (TARGET_ENTITIES / BENCHMARK_ENTITIES) * BENCHMARK_INDEX_TIME / 3600
    print(f"  Single-threaded: {single_thread_hours:.1f} hours")

    for workers in [4, 8, 16, 32]:
        hours = single_thread_hours / workers
        print(f"  {workers} workers: {hours:.1f} hours")

    # Memory (in-memory approach - NOT recommended)
    print("\n💾 MEMORY (in-memory, NOT recommended)")
    memory_gb = (TARGET_ENTITIES / BENCHMARK_ENTITIES) * BENCHMARK_MEMORY_KB / 1024 / 1024
    print(f"  Entity dict: ~{memory_gb:.1f} GB")

    embedding_gb = TARGET_ENTITIES * 768 * 4 / 1024 / 1024 / 1024  # 768 dims, 4 bytes each
    print(f"  Embeddings: ~{embedding_gb:.1f} GB")
    print(f"  Total RAM needed: ~{memory_gb + embedding_gb:.0f} GB")

    # OpenSearch (recommended)
    print("\n🔍 OPENSEARCH (recommended)")
    doc_size_kb = 2  # avg doc size with embedding
    storage_gb = TARGET_ENTITIES * doc_size_kb / 1024 / 1024
    print(f"  Storage: ~{storage_gb:.0f} GB")
    print(f"  Recommended: 3 data nodes, 64GB RAM each")
    print(f"  Shards: 30 (1M docs per shard)")

    # Search latency
    print("\n⚡ SEARCH LATENCY (with OpenSearch)")
    print(f"  BM25 pre-filter: ~100ms")
    print(f"  Vector search (top-100): ~200ms")
    print(f"  LLM re-rank: ~1-2s")
    print(f"  Total: ~2-3s per query")

    # Cost estimate (AWS)
    print("\n💰 AWS COST ESTIMATE (monthly)")
    print(f"  OpenSearch (3x r6g.2xlarge): ~$1,500")
    print(f"  ECS Fargate (2 tasks): ~$150")
    print(f"  Bedrock (1000 queries/day): ~$300")
    print(f"  Total: ~$2,000/month")

    # Recommendations
    print("\n✅ RECOMMENDATIONS")
    print(f"  1. Use OpenSearch, not in-memory")
    print(f"  2. Index with 16+ parallel workers")
    print(f"  3. Shard by package/repo for faster queries")
    print(f"  4. Pre-filter using stack trace (30M → 10K)")
    print(f"  5. Cache frequent queries in Redis")


if __name__ == "__main__":
    estimate()
