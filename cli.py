#!/usr/bin/env python3
"""
CodeWise CLI - AI-powered fault localization tool.

Usage:
    # Index a codebase
    python cli.py /path/to/codebase --index-only

    # Find fault from stack trace
    python cli.py /path/to/codebase --error "NullPointerException at..."

    # Natural language code search
    python cli.py /path/to/codebase --query "where do we handle pricing?"

    # Generate solution for a bug
    python cli.py /path/to/codebase --query "bug description" --solve

    # Multi-repo: index all packages in a parent directory
    python cli.py /path/to/workspace --multi-repo --query "payment validation"

    # Provide feedback to improve future searches
    python cli.py /path/to/codebase --query "pricing bug" --feedback /path/to/actual/file.java
"""

import argparse
import sys
from pathlib import Path


def print_results(results: list[dict], header: str, show_repo: bool = False) -> None:
    """Pretty print search results."""
    print(f"\n{'=' * 60}")
    print(header)
    print('=' * 60)

    for i, result in enumerate(results, 1):
        entity = result["entity"]
        repo = result.get("repo", "")
        
        print(f"\n{i}. {entity.full_name}")
        if show_repo and repo:
            print(f"   Repo: {repo}")
        print(f"   File: {entity.file_path}:{entity.start_line}-{entity.end_line}")
        print(f"   Type: {entity.entity_type.value}")
        
        if result.get("confidence"):
            print(f"   Confidence: {result['confidence']:.0%}")
        if result.get("reason"):
            print(f"   Reason: {result['reason']}")
        
        print(f"   Signature: {entity.signature}")


def run_single_repo(args, codebase_path: str):
    """Run fault localization on a single repo."""
    from fault_localizer import FaultLocalizer
    from indexer.local_cache import LocalIndexCache
    
    cache = LocalIndexCache()
    cached_data = None if args.force_reindex else cache.get(codebase_path)
    localizer = FaultLocalizer(codebase_path, use_llm=not args.no_llm)

    if cached_data:
        print(f"Using cached index ({cached_data['count']} entities)")
        localizer.load_from_cache(cached_data)
    else:
        print(f"Indexing {codebase_path}...")
        count = localizer.index()
        print(f"Indexed {count} entities")
        cache.set(codebase_path, localizer.get_cache_data())

    return localizer


def run_multi_repo(args, parent_path: str):
    """Run fault localization across multiple repos."""
    from indexer.multi_repo_indexer import MultiRepoIndexer
    
    indexer = MultiRepoIndexer()
    added = indexer.add_repos_from_parent(parent_path)
    print(f"Found {added} repositories")
    
    if added == 0:
        print("No code repositories found in subdirectories")
        sys.exit(1)
    
    stats = indexer.index_all(force=args.force_reindex)
    total = sum(stats.values())
    print(f"Total: {total} entities across {len(stats)} repos")
    
    return indexer


def main():
    parser = argparse.ArgumentParser(
        description="CodeWise - AI-powered fault localization",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    
    # Required
    parser.add_argument("codebase", help="Path to codebase or parent directory (with --multi-repo)")
    
    # Query options
    parser.add_argument("-e", "--error", help="Stack trace or error message")
    parser.add_argument("-f", "--error-file", help="File containing stack trace")
    parser.add_argument("-q", "--query", help="Natural language query")
    
    # Options
    parser.add_argument("-k", "--top-k", type=int, default=5, help="Number of results (default: 5)")
    parser.add_argument("-s", "--solve", action="store_true", help="Generate solution with LLM")
    parser.add_argument("--no-llm", action="store_true", help="Skip LLM re-ranking")
    parser.add_argument("--index-only", action="store_true", help="Only index, don't search")
    parser.add_argument("--force-reindex", action="store_true", help="Force re-indexing")
    parser.add_argument("--feedback", metavar="FILE", help="Correct file path (improves future searches)")
    parser.add_argument("--multi-repo", action="store_true", help="Index all subdirs as separate repos")
    parser.add_argument("--stats", action="store_true", help="Show feedback learning stats")

    args = parser.parse_args()
    codebase_path = str(Path(args.codebase).resolve())

    # Show feedback stats
    if args.stats:
        from feedback import FeedbackStore
        store = FeedbackStore()
        stats = store.get_stats()
        print(f"Feedback Stats:")
        print(f"  Total corrections: {stats['total']}")
        print(f"  Learned keywords: {stats.get('learned_keywords', 0)}")
        if stats['total'] > 0:
            print(f"  Initial accuracy: {stats.get('accuracy', 0):.0%}")
        return

    # Index
    if args.multi_repo:
        indexer = run_multi_repo(args, codebase_path)
        is_multi = True
    else:
        localizer = run_single_repo(args, codebase_path)
        is_multi = False

    if args.index_only:
        return

    # Determine query
    if args.query:
        query_text = args.query
        mode = "search"
    elif args.error_file:
        query_text = Path(args.error_file).read_text()
        mode = "localize"
    elif args.error:
        query_text = args.error
        mode = "localize"
    else:
        print("Error: Provide --query, --error, or --error-file")
        sys.exit(1)

    # Execute search
    print(f"\n{'Searching' if mode == 'search' else 'Localizing'}: {query_text[:80]}...")
    
    if is_multi:
        raw_results = indexer.search(query_text, top_k=args.top_k)
        results = [{"entity": e, "score": s, "repo": r} for e, s, r in raw_results]
        header = "MULTI-REPO SEARCH RESULTS"
    else:
        if mode == "search":
            results = localizer.search(query_text, top_k=args.top_k)
            header = "CODE SEARCH RESULTS"
        else:
            results = localizer.localize(query_text, top_k=args.top_k)
            header = "SUSPECTED FAULT LOCATIONS"

    print_results(results, header, show_repo=is_multi)

    # Handle feedback
    if args.feedback and results:
        from feedback import FeedbackStore
        store = FeedbackStore()
        predicted = [r["entity"].file_path for r in results]
        store.add_feedback(query_text, predicted, args.feedback)
        print(f"\n✓ Feedback recorded. Future searches for similar queries will be improved.")

    # Generate solution
    if args.solve and results:
        print(f"\n{'=' * 60}")
        print("GENERATING SOLUTION...")
        print('=' * 60)
        
        from ranker.solution_generator import SolutionGenerator
        solver = SolutionGenerator()
        solution = solver.generate_solution(
            error_description=query_text,
            candidates=results,
            codebase_path=codebase_path
        )
        print(f"\n{solution['raw_analysis']}")


if __name__ == "__main__":
    main()
