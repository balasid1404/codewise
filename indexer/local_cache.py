"""Local file-based cache for indexed entities."""

import pickle
import hashlib
from pathlib import Path
from typing import Optional


class LocalIndexCache:
    """Cache indexed entities to disk to avoid re-indexing."""

    CACHE_DIR = Path.home() / ".fault-localizer" / "cache"

    def __init__(self):
        self.CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def _get_cache_key(self, codebase_path: str) -> str:
        """Generate cache key from codebase path."""
        return hashlib.md5(codebase_path.encode()).hexdigest()

    def _get_cache_path(self, codebase_path: str) -> Path:
        """Get cache file path."""
        key = self._get_cache_key(codebase_path)
        return self.CACHE_DIR / f"{key}.pkl"

    def get(self, codebase_path: str) -> Optional[dict]:
        """Load cached index if exists."""
        cache_path = self._get_cache_path(codebase_path)
        if cache_path.exists():
            try:
                with open(cache_path, "rb") as f:
                    return pickle.load(f)
            except Exception:
                return None
        return None

    def set(self, codebase_path: str, data: dict) -> None:
        """Save index to cache."""
        cache_path = self._get_cache_path(codebase_path)
        try:
            with open(cache_path, "wb") as f:
                pickle.dump(data, f)
        except Exception:
            pass

    def invalidate(self, codebase_path: str) -> None:
        """Remove cached index."""
        cache_path = self._get_cache_path(codebase_path)
        if cache_path.exists():
            cache_path.unlink()

    def clear_all(self) -> None:
        """Clear all cached indexes."""
        for f in self.CACHE_DIR.glob("*.pkl"):
            f.unlink()
