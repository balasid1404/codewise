"""Webhook handler for auto-reindex on git push."""

import hmac
import hashlib
from typing import Optional


class GitWebhookHandler:
    """Handle GitHub/GitLab webhooks for auto-reindexing."""

    def __init__(self, secret: Optional[str] = None):
        self.secret = secret

    def verify_github_signature(self, payload: bytes, signature: str) -> bool:
        """Verify GitHub webhook signature."""
        if not self.secret:
            return True  # No secret configured, skip verification

        expected = "sha256=" + hmac.new(
            self.secret.encode(),
            payload,
            hashlib.sha256
        ).hexdigest()

        return hmac.compare_digest(expected, signature)

    def parse_github_push(self, payload: dict) -> dict:
        """Parse GitHub push event."""
        return {
            "repo": payload.get("repository", {}).get("full_name"),
            "branch": payload.get("ref", "").replace("refs/heads/", ""),
            "commits": len(payload.get("commits", [])),
            "changed_files": self._extract_changed_files(payload),
            "pusher": payload.get("pusher", {}).get("name")
        }

    def parse_gitlab_push(self, payload: dict) -> dict:
        """Parse GitLab push event."""
        return {
            "repo": payload.get("project", {}).get("path_with_namespace"),
            "branch": payload.get("ref", "").replace("refs/heads/", ""),
            "commits": len(payload.get("commits", [])),
            "changed_files": self._extract_gitlab_changed_files(payload),
            "pusher": payload.get("user_name")
        }

    def _extract_changed_files(self, payload: dict) -> list[str]:
        """Extract changed files from GitHub payload."""
        files = set()
        for commit in payload.get("commits", []):
            files.update(commit.get("added", []))
            files.update(commit.get("modified", []))
            files.update(commit.get("removed", []))
        return [f for f in files if f.endswith((".py", ".java"))]

    def _extract_gitlab_changed_files(self, payload: dict) -> list[str]:
        """Extract changed files from GitLab payload."""
        files = set()
        for commit in payload.get("commits", []):
            files.update(commit.get("added", []))
            files.update(commit.get("modified", []))
            files.update(commit.get("removed", []))
        return [f for f in files if f.endswith((".py", ".java"))]

    def should_reindex(self, changed_files: list[str]) -> bool:
        """Determine if push warrants re-indexing."""
        # Reindex if any Python or Java files changed
        return len(changed_files) > 0
