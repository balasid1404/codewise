"""Redis caching for query results."""

import json
import hashlib
from typing import Optional, Any
import redis


class QueryCache:
    """Cache query results in Redis."""

    def __init__(self, host: str = "localhost", port: int = 6379, ttl: int = 3600):
        self.ttl = ttl
        try:
            self.client = redis.Redis(host=host, port=port, decode_responses=True)
            self.client.ping()
            self.enabled = True
        except redis.ConnectionError:
            self.enabled = False
            self.client = None

    def _make_key(self, query: str, query_type: str) -> str:
        """Generate cache key from query."""
        hash_val = hashlib.md5(query.encode()).hexdigest()[:16]
        return f"fault_loc:{query_type}:{hash_val}"

    def get(self, query: str, query_type: str = "localize") -> Optional[list[dict]]:
        """Get cached results."""
        if not self.enabled:
            return None

        key = self._make_key(query, query_type)
        try:
            data = self.client.get(key)
            if data:
                return json.loads(data)
        except Exception:
            pass
        return None

    def set(self, query: str, results: list[dict], query_type: str = "localize") -> None:
        """Cache results."""
        if not self.enabled:
            return

        key = self._make_key(query, query_type)
        try:
            # Serialize results (exclude non-serializable entity objects)
            serializable = []
            for r in results:
                entity = r.get("entity")
                serializable.append({
                    "name": entity.name if entity else "",
                    "full_name": entity.full_name if entity else "",
                    "file_path": entity.file_path if entity else "",
                    "start_line": entity.start_line if entity else 0,
                    "end_line": entity.end_line if entity else 0,
                    "signature": entity.signature if entity else "",
                    "confidence": r.get("confidence", 0),
                    "reason": r.get("reason", ""),
                    "score": r.get("score", 0)
                })
            self.client.setex(key, self.ttl, json.dumps(serializable))
        except Exception:
            pass

    def invalidate(self, pattern: str = "*") -> int:
        """Invalidate cached entries."""
        if not self.enabled:
            return 0

        try:
            keys = self.client.keys(f"fault_loc:{pattern}")
            if keys:
                return self.client.delete(*keys)
        except Exception:
            pass
        return 0
