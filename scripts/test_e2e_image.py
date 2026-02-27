#!/usr/bin/env python3
"""End-to-end test for image-based fault localization."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from fault_localizer import FaultLocalizer
from extractors.ui_mapper import UIMapper


def test_image_localization_e2e():
    """
    Test image-based localization against sample payment code.
    
    Simulates: User sees "Payment could not be processed" error on checkout page.
    Expected: Find PaymentService.process_payment, BillingClient.charge, etc.
    """
    print("=" * 60)
    print("E2E TEST: Image-Based Fault Localization")
    print("=" * 60)

    # 1. Index the sample repo
    print("\n📁 Indexing sample-repo...")
    localizer = FaultLocalizer(
        codebase_path=str(Path(__file__).parent.parent / "sample-repo"),
        use_llm=False  # Skip LLM for local testing
    )
    count = localizer.index()
    print(f"   Indexed {count} entities")

    # 2. Simulate image extraction (what Claude Vision would return)
    print("\n📸 Simulating image extraction...")
    extracted = {
        "error_message": "Payment could not be processed. Please try again.",
        "ui_elements": ["Pay Now", "Credit Card", "Total: $99.99", "Checkout"],
        "app_section": "payment",
        "user_action": "completing purchase",
        "keywords": ["payment", "checkout", "billing"]
    }
    print(f"   Error: {extracted['error_message']}")
    print(f"   Section: {extracted['app_section']}")

    # 3. Map to code patterns
    print("\n🔍 Mapping UI to code patterns...")
    mapper = UIMapper()
    context = mapper.build_search_context(extracted)
    print(f"   Patterns: {context['code_patterns'][:10]}")

    # 4. Search for matching code
    print("\n🎯 Searching for fault locations...")

    # Build query from extracted data
    query_parts = [extracted["error_message"]] + context["code_patterns"][:10]
    query = " ".join(query_parts)

    # Use the retriever directly
    results = localizer.retriever.search(query, top_k=10)

    print("\n" + "=" * 60)
    print("RESULTS")
    print("=" * 60)

    found_payment = False
    for i, (entity, score) in enumerate(results, 1):
        print(f"\n{i}. {entity.full_name}")
        print(f"   File: {entity.file_path}")
        print(f"   Lines: {entity.start_line}-{entity.end_line}")
        print(f"   Score: {score:.3f}")
        print(f"   Signature: {entity.signature}")

        if "payment" in entity.file_path.lower() or "pay" in entity.name.lower():
            found_payment = True

    print("\n" + "=" * 60)
    if found_payment:
        print("✅ SUCCESS: Found payment-related code from UI context!")
    else:
        print("⚠️  Payment code not in top results (may need tuning)")
    print("=" * 60)

    # Show what we expected to find
    print("\n📋 Expected matches in sample-repo/app/payment/:")
    print("   - PaymentService.process_payment")
    print("   - PaymentService.pay_now")
    print("   - CheckoutController.handle_checkout")
    print("   - BillingClient.charge")
    print("   - PaymentValidator.validate_card")


if __name__ == "__main__":
    test_image_localization_e2e()
