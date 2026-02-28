"""Retry utilities for resilient operations."""

import time
import functools
from typing import Callable, Type, Tuple


def retry(
    max_attempts: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: Tuple[Type[Exception], ...] = (Exception,)
):
    """
    Retry decorator with exponential backoff.
    
    Args:
        max_attempts: Maximum retry attempts
        delay: Initial delay between retries (seconds)
        backoff: Multiplier for delay after each retry
        exceptions: Tuple of exceptions to catch and retry
    """
    def decorator(func: Callable):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            current_delay = delay
            last_exception = None

            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_attempts - 1:
                        time.sleep(current_delay)
                        current_delay *= backoff

            raise last_exception

        return wrapper
    return decorator


def wait_for_opensearch(client, max_wait: int = 60, interval: int = 5) -> bool:
    """Wait for OpenSearch to be ready."""
    start = time.time()
    while time.time() - start < max_wait:
        try:
            client.cluster.health(wait_for_status="yellow", timeout="5s")
            return True
        except Exception:
            time.sleep(interval)
    return False
