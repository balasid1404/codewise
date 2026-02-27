#!/usr/bin/env python3
"""CLI for fault localization."""

import argparse
import sys
from pathlib import Path
from fault_localizer import FaultLocalizer


def main():
    parser = argparse.ArgumentParser(description="Fault Localization Tool")
    parser.add_argument("codebase", help="Path to codebase directory")
    parser.add_argument("--error-file", "-e", help="File containing stack trace")
    parser.add_argument("--error", help="Stack trace string directly")
    parser.add_argument("--top-k", "-k", type=int, default=5, help="Number of results")
    parser.add_argument("--no-llm", action="store_true", help="Skip LLM re-ranking")
    parser.add_argument("--index-only", action="store_true", help="Only index, don't localize")

    args = parser.parse_args()

    localizer = FaultLocalizer(args.codebase, use_llm=not args.no_llm)

    print(f"Indexing {args.codebase}...")
    count = localizer.index()
    print(f"Indexed {count} entities")

    if args.index_only:
        return

    # Get error text
    if args.error_file:
        error_text = Path(args.error_file).read_text()
    elif args.error:
        error_text = args.error
    else:
        print("Reading stack trace from stdin...")
        error_text = sys.stdin.read()

    if not error_text.strip():
        print("No error text provided")
        sys.exit(1)

    print("\nLocalizing fault...")
    results = localizer.localize(error_text, top_k=args.top_k)

    print(f"\n{'='*60}")
    print("SUSPECTED FAULT LOCATIONS")
    print('='*60)

    for i, result in enumerate(results, 1):
        entity = result["entity"]
        print(f"\n{i}. {entity.full_name}")
        print(f"   Location: {entity.file_path}:{entity.start_line}-{entity.end_line}")
        print(f"   Type: {entity.entity_type.value}")
        if result.get("confidence"):
            print(f"   Confidence: {result['confidence']:.0%}")
        if result.get("reason"):
            print(f"   Reason: {result['reason']}")
        print(f"   Signature: {entity.signature}")


if __name__ == "__main__":
    main()
