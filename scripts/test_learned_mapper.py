#!/usr/bin/env python3
"""Test the learned UI mapper."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from fault_localizer import FaultLocalizer
from extractors.learned_ui_mapper import LearnedUIMapper


def test_learned_mapper():
    print("=" * 60)
    print("LEARNED UI MAPPER TEST")
    print("=" * 60)

    # 1. Index sample repo
    print("\n📁 Indexing sample-repo...")
    localizer = FaultLocalizer(
        codebase_path=str(Path(__file__).parent.parent / "sample-repo"),
        use_llm=False
    )
    count = localizer.index()
    print(f"   Indexed {count} entities")

    # 2. Build learned mapper from indexed entities
    print("\n📚 Building learned vocabulary...")
    mapper = LearnedUIMapper()
    entities = list(localizer.indexer.entities.values())
    mapper.learn_from_entities(entities)

    stats = mapper.get_stats()
    print(f"   Entities: {stats['total_entities']}")
    print(f"   Vocabulary size: {stats['vocabulary_size']}")
    print(f"   Top words: {[w for w, _ in stats['top_words'][:10]]}")

    # 3. Test UI text -> code pattern mapping
    print("\n🔍 Testing UI -> Code mappings:")

    test_cases = [
        "Pay Now",
        "Payment",
        "Checkout",
        "Validate",
        "Credit Card",
        "Process",
        "Billing",
    ]

    for ui_text in test_cases:
        patterns = mapper.get_code_patterns(ui_text)
        print(f"\n   '{ui_text}' →")
        for p in patterns[:5]:
            print(f"      - {p}")

    # 4. Test full search context
    print("\n" + "=" * 60)
    print("FULL SEARCH CONTEXT TEST")
    print("=" * 60)

    extracted = {
        "error_message": "Payment could not be processed",
        "ui_elements": ["Pay Now", "Credit Card", "Checkout"],
        "app_section": "payment",
        "user_action": "completing purchase",
        "keywords": ["payment", "billing"]
    }

    context = mapper.build_search_context(extracted)

    print("\n📸 Extracted from image:")
    print(f"   Error: {extracted['error_message']}")
    print(f"   UI Elements: {extracted['ui_elements']}")

    print("\n🎯 Learned code patterns:")
    for p in context["code_patterns"][:15]:
        print(f"   - {p}")

    print("\n📁 File patterns:")
    for p in context["file_patterns"]:
        print(f"   - {p}")

    # 5. Compare with hardcoded mapper
    print("\n" + "=" * 60)
    print("COMPARISON: Learned vs Hardcoded")
    print("=" * 60)

    from extractors.ui_mapper import UIMapper
    hardcoded = UIMapper()

    ui_text = "Pay Now"
    learned_patterns = mapper.get_code_patterns(ui_text)
    hardcoded_patterns = hardcoded.get_code_patterns(ui_text)

    print(f"\n   UI Text: '{ui_text}'")
    print(f"\n   Hardcoded patterns: {hardcoded_patterns[:5]}")
    print(f"\n   Learned patterns: {learned_patterns[:5]}")

    print("\n✅ Learned mapper uses actual code from your codebase!")
    print("   No hardcoded mappings needed.")


if __name__ == "__main__":
    test_learned_mapper()
