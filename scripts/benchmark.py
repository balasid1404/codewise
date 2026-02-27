#!/usr/bin/env python3
"""Benchmark fault localization at scale."""

import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fault_localizer import FaultLocalizer

TEST_CODEBASE = Path(__file__).parent.parent / "test-codebase"

SAMPLE_ERRORS = [
    """Traceback (most recent call last):
  File "python/user/service/user_service_0.py", line 15, in processUser
    result = self._validate(data)
  File "python/user/service/user_service_0.py", line 22, in _validate
    raise ValueError("Invalid user data")
ValueError: Invalid user data""",

    """java.lang.IllegalArgumentException: Invalid order data
    at com.example.service.order.OrderService0.processOrder(OrderService0.java:25)
    at com.example.controller.order.OrderController0.handleOrder(OrderController0.java:18)
    at com.example.handler.order.OrderHandler0.executeOrder(OrderHandler0.java:12)""",

    """Traceback (most recent call last):
  File "python/payment/processor/payment_processor_2.py", line 15, in processPayment
    result = self._validate(data)
  File "python/payment/validator/payment_validator_1.py", line 22, in _validate
    raise ValueError("Invalid payment data")
ValueError: Invalid payment data""",
]


def benchmark():
    print(f"Test codebase: {TEST_CODEBASE}")
    print(f"Files: 1000 (500 Python + 500 Java)\n")

    localizer = FaultLocalizer(str(TEST_CODEBASE), use_llm=False)

    # Benchmark indexing
    print("=" * 50)
    print("INDEXING BENCHMARK")
    print("=" * 50)

    start = time.time()
    count = localizer.index()
    index_time = time.time() - start

    print(f"Entities indexed: {count}")
    print(f"Index time: {index_time:.2f}s")
    print(f"Throughput: {count / index_time:.0f} entities/sec")

    # Benchmark search
    print("\n" + "=" * 50)
    print("SEARCH BENCHMARK")
    print("=" * 50)

    for i, error in enumerate(SAMPLE_ERRORS, 1):
        print(f"\nError {i}:")
        start = time.time()
        results = localizer.localize(error, top_k=5)
        search_time = time.time() - start

        print(f"  Search time: {search_time:.3f}s")
        print(f"  Results: {len(results)}")
        if results:
            top = results[0]["entity"]
            print(f"  Top result: {top.full_name} ({top.file_path})")

    # Memory estimate
    print("\n" + "=" * 50)
    print("MEMORY ESTIMATE")
    print("=" * 50)

    import sys
    entities_size = sys.getsizeof(localizer.indexer.entities)
    print(f"Entities dict: ~{entities_size / 1024:.1f} KB")


if __name__ == "__main__":
    benchmark()
