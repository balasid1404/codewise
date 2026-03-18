#!/usr/bin/env python3
"""Local indexing script: parse + resolve + embed on local machine, push to remote API.

Usage:
    python scripts/local_index.py /path/to/codebase --api http://FaultL-Fault-XibxofP5guVC-1959899228.us-east-1.elb.amazonaws.com --namespace my-project

This runs the CPU-heavy embedding on your local machine (14 cores >> 2 vCPU Fargate)
and pushes the pre-embedded entities to the remote OpenSearch via the bulk-import API.
"""

import sys
import os
import json
import time
import argparse
import requests

# Add parent dir to path so we can import project modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from sentence_transformers import SentenceTransformer

from indexer.python_parser import PythonParser
from indexer.java_parser import JavaParser
from indexer.js_ts_parser import JsTsParser
from indexer.html_parser import HtmlParser
from indexer.relationship_resolver import RelationshipResolver


def parse_codebase(codebase_path: str, workers: int = 8):
    """Parse all source files into CodeEntity objects."""
    codebase = Path(codebase_path)
    skip_dirs = {"venv", "node_modules", ".git", "__pycache__", "build", "dist", "cdk.out"}
    all_files = [
        f for f in codebase.rglob("*")
        if f.suffix in (".py", ".java", ".js", ".ts", ".html")
        and not any(d in f.parts for d in skip_dirs)
    ]

    print(f"Found {len(all_files)} source files")

    python_parser = PythonParser()
    java_parser = JavaParser()
    js_ts_parser = JsTsParser()
    html_parser = HtmlParser()

    def parse_file(f):
        try:
            if f.suffix == ".py":
                return python_parser.parse_file(f)
            elif f.suffix == ".java":
                return java_parser.parse_file(f)
            elif f.suffix in (".js", ".ts"):
                return js_ts_parser.parse_file(f)
            elif f.suffix == ".html":
                return html_parser.parse_file(f)
        except Exception as e:
            print(f"  Parse error {f}: {e}")
        return []

    entities = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(parse_file, f): f for f in all_files}
        done = 0
        for future in as_completed(futures):
            done += 1
            try:
                ents = future.result()
                entities.extend(ents)
            except Exception as e:
                print(f"  Future error: {e}")
            if done % 50 == 0 or done == len(all_files):
                print(f"  Parsed {done}/{len(all_files)} files → {len(entities)} entities")

    return entities


def embed_entities(entities, model_name="microsoft/codebert-base", batch_size=256):
    """Embed all entities using local CPU."""
    import numpy as np

    print(f"Loading model {model_name}...")
    t0 = time.time()
    encoder = SentenceTransformer(model_name)
    print(f"Model loaded in {time.time()-t0:.1f}s")

    t0 = time.time()
    trivial = 0
    to_embed_single = []
    to_embed_chunked = []

    for ent in entities:
        body_len = len(ent.body) if ent.body else 0
        if body_len < 30:
            ent.embedding = [0.0] * 768
            trivial += 1
            continue
        chunks = ent.to_embedding_chunks(chunk_size=512, overlap=64)
        if len(chunks) <= 1:
            to_embed_single.append((ent, chunks[0] if chunks else ent.to_embedding_text()))
        else:
            to_embed_chunked.append((ent, chunks))

    print(f"Trivial (skipped): {trivial}, Single-chunk: {len(to_embed_single)}, Multi-chunk: {len(to_embed_chunked)}")

    # Batch encode single-chunk
    if to_embed_single:
        texts = [t for _, t in to_embed_single]
        print(f"Encoding {len(texts)} single-chunk entities...")
        embeddings = encoder.encode(texts, show_progress_bar=True, batch_size=batch_size)
        for (ent, _), emb in zip(to_embed_single, embeddings):
            ent.embedding = emb.tolist()

    # Batch encode multi-chunk
    if to_embed_chunked:
        all_chunks = []
        chunk_map = []
        for i, (ent, chunks) in enumerate(to_embed_chunked):
            all_chunks.extend(chunks)
            chunk_map.append((i, len(chunks)))
        print(f"Encoding {len(all_chunks)} chunks from {len(to_embed_chunked)} multi-chunk entities...")
        all_embeddings = encoder.encode(all_chunks, show_progress_bar=True, batch_size=batch_size)
        offset = 0
        for ent_idx, count in chunk_map:
            ent = to_embed_chunked[ent_idx][0]
            chunk_embs = all_embeddings[offset:offset + count]
            ent.embedding = np.mean(chunk_embs, axis=0).tolist()
            offset += count

    elapsed = time.time() - t0
    print(f"Embedding done in {elapsed:.1f}s ({elapsed/len(entities)*1000:.1f}ms per entity)")


def push_to_api(entities, api_url, namespace, batch_size=200):
    """Push pre-embedded entities to remote API in batches."""
    total = len(entities)
    pushed = 0

    for i in range(0, total, batch_size):
        batch = entities[i:i + batch_size]
        payload = {
            "namespace": namespace,
            "entities": [
                {
                    "id": e.id,
                    "name": e.name,
                    "entity_type": e.entity_type.value,
                    "file_path": e.file_path,
                    "start_line": e.start_line,
                    "end_line": e.end_line,
                    "signature": e.signature,
                    "body": e.body[:5000] if e.body else "",
                    "class_name": e.class_name,
                    "package": e.package,
                    "docstring": e.docstring,
                    "embedding": e.embedding,
                    "calls": e.calls,
                    "imports": e.imports,
                    "annotations": e.annotations,
                    "resolved_calls": e.resolved_calls,
                    "base_classes": e.base_classes,
                    "file_imports": e.file_imports,
                }
                for e in batch
            ],
        }
        r = requests.post(f"{api_url}/index/bulk-import", json=payload, timeout=120)
        r.raise_for_status()
        pushed += len(batch)
        print(f"  Pushed {pushed}/{total} entities")

    print(f"Done! {pushed} entities imported to namespace '{namespace}'")


def main():
    parser = argparse.ArgumentParser(description="Local index + push to remote API")
    parser.add_argument("codebase", help="Path to codebase directory")
    parser.add_argument("--api", required=True, help="Remote API URL (e.g. http://FaultL-...elb.amazonaws.com)")
    parser.add_argument("--namespace", default=None, help="Namespace (default: directory name)")
    parser.add_argument("--workers", type=int, default=8, help="Parse workers")
    parser.add_argument("--model", default="microsoft/codebert-base", help="Embedding model")
    args = parser.parse_args()

    namespace = args.namespace or Path(args.codebase).name

    print(f"=== Local Index: {args.codebase} → {args.api} (namespace: {namespace}) ===\n")

    # Stage 1: Parse
    t0 = time.time()
    entities = parse_codebase(args.codebase, workers=args.workers)
    print(f"\n✓ Parsed {len(entities)} entities in {time.time()-t0:.1f}s\n")

    if not entities:
        print("No entities found.")
        return

    # Set namespace
    for e in entities:
        e.namespace = namespace

    # Stage 2: Resolve
    t0 = time.time()
    resolver = RelationshipResolver()
    resolver.resolve(entities)
    print(f"✓ Resolved relationships in {time.time()-t0:.1f}s\n")

    # Stage 3: Embed
    embed_entities(entities, model_name=args.model)
    print()

    # Stage 4: Push
    print(f"Pushing to {args.api}...")
    push_to_api(entities, args.api, namespace)


if __name__ == "__main__":
    main()
