"""Incremental indexing - only index changed files."""

import hashlib
from pathlib import Path
from typing import Optional
from datetime import datetime


class IncrementalIndexer:
    """Track file changes and index only what's new/modified."""

    def __init__(self, opensearch_client):
        self.client = opensearch_client
        self.index_name = "file_hashes"
        self._ensure_index()

    def _ensure_index(self):
        """Create hash tracking index."""
        if not self.client.indices.exists(self.index_name):
            self.client.indices.create(
                index=self.index_name,
                body={
                    "mappings": {
                        "properties": {
                            "file_path": {"type": "keyword"},
                            "content_hash": {"type": "keyword"},
                            "indexed_at": {"type": "date"},
                            "entity_count": {"type": "integer"}
                        }
                    }
                }
            )

    def get_file_hash(self, file_path: Path) -> str:
        """Compute hash of file content."""
        content = file_path.read_bytes()
        return hashlib.sha256(content).hexdigest()

    def needs_reindex(self, file_path: Path) -> bool:
        """Check if file needs re-indexing."""
        current_hash = self.get_file_hash(file_path)
        stored_hash = self._get_stored_hash(str(file_path))
        return current_hash != stored_hash

    def _get_stored_hash(self, file_path: str) -> Optional[str]:
        """Get previously stored hash."""
        try:
            response = self.client.get(index=self.index_name, id=file_path)
            return response["_source"].get("content_hash")
        except Exception:
            return None

    def mark_indexed(self, file_path: Path, entity_count: int) -> None:
        """Record that file has been indexed."""
        self.client.index(
            index=self.index_name,
            id=str(file_path),
            body={
                "file_path": str(file_path),
                "content_hash": self.get_file_hash(file_path),
                "indexed_at": datetime.utcnow().isoformat(),
                "entity_count": entity_count
            }
        )

    def get_changed_files(self, files: list[Path]) -> list[Path]:
        """Filter to only files that need re-indexing."""
        return [f for f in files if self.needs_reindex(f)]

    def get_stats(self) -> dict:
        """Get indexing statistics."""
        try:
            response = self.client.count(index=self.index_name)
            return {"indexed_files": response["count"]}
        except Exception:
            return {"indexed_files": 0}
