"""Storage backends for production scale."""

from .opensearch_store import OpenSearchStore

__all__ = ["OpenSearchStore"]
