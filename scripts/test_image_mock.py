#!/usr/bin/env python3
"""Test image localization with mock data (no actual image needed)."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from extractors.ui_mapper import UIMapper


def simulate_image_localization():
    """Simulate what happens when you pass a payment error screenshot."""

    print("=" * 60)
    print("SIMULATED IMAGE LOCALIZATION")
    print("=" * 60)

    # This is what Claude Vision would extract from a payment error screenshot
    extracted = {
        "error_message": "Payment could not be processed. Please try again.",
        "ui_elements": ["Pay Now", "Credit Card", "Total: $99.99", "Checkout", "Cancel"],
        "app_section": "payment",
        "user_action": "attempting to complete payment for music subscription",
        "keywords": ["payment", "transaction", "checkout", "billing", "credit card"],
        "raw_text": "Music App - Checkout - Payment Error - Payment could not be processed. Please try again. Pay Now Credit Card **** 1234 Total: $99.99 Cancel"
    }

    print("\n📸 EXTRACTED FROM IMAGE:")
    print(f"  Error: {extracted['error_message']}")
    print(f"  Section: {extracted['app_section']}")
    print(f"  UI Elements: {extracted['ui_elements']}")
    print(f"  User Action: {extracted['user_action']}")

    # Map to code patterns
    mapper = UIMapper()
    context = mapper.build_search_context(extracted)

    print("\n🔍 GENERATED CODE PATTERNS:")
    for pattern in context["code_patterns"][:15]:
        print(f"  - {pattern}")

    print("\n📁 FILE PATTERNS TO SEARCH:")
    for pattern in context["file_patterns"]:
        print(f"  - {pattern}")

    # Build search query
    query_parts = context["code_patterns"][:10] + [extracted["error_message"]]
    query = " ".join(query_parts)

    print(f"\n🔎 SEARCH QUERY:")
    print(f"  {query[:100]}...")

    print("\n✅ EXPECTED RESULTS (if codebase has payment code):")
    print("  1. PaymentService.processPayment - handles payment flow")
    print("  2. CheckoutController.handlePayment - API endpoint")
    print("  3. PaymentValidator.validateCard - validation logic")
    print("  4. BillingService.charge - actual charge")

    print("\n" + "=" * 60)
    print("To test with real image + OpenSearch:")
    print("  1. docker-compose up -d")
    print("  2. Index your codebase: POST /index")
    print("  3. POST /localize/image with your screenshot path")
    print("=" * 60)


if __name__ == "__main__":
    simulate_image_localization()
