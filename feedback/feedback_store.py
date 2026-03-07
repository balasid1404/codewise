"""
Feedback loop - learn from user corrections to improve search.

When user says "actually the bug was in X.java, not Y.java", we store that
and use it to boost similar queries in the future.
"""

import json
import hashlib
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class Feedback:
    """A single feedback entry."""
    query: str
    query_hash: str
    predicted: list[str]  # What we predicted (file paths)
    actual: str           # What user said was correct
    timestamp: str
    helpful_keywords: list[str]  # Keywords that should have matched


class FeedbackStore:
    """
    Stores and learns from user feedback.
    
    Learning strategy:
    1. Store query → actual_file mappings
    2. Extract keywords from successful matches
    3. Boost entities that match learned patterns
    """
    
    STORE_PATH = Path.home() / ".fault-localizer" / "feedback.json"
    
    def __init__(self):
        self.feedbacks: list[Feedback] = []
        self.keyword_boosts: dict[str, list[str]] = {}  # keyword → [file patterns]
        self._load()
    
    def _load(self) -> None:
        """Load feedback from disk."""
        if self.STORE_PATH.exists():
            try:
                data = json.loads(self.STORE_PATH.read_text())
                self.feedbacks = [Feedback(**f) for f in data.get("feedbacks", [])]
                self.keyword_boosts = data.get("keyword_boosts", {})
            except Exception:
                self.feedbacks = []
                self.keyword_boosts = {}
    
    def _save(self) -> None:
        """Persist feedback to disk."""
        self.STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "feedbacks": [asdict(f) for f in self.feedbacks],
            "keyword_boosts": self.keyword_boosts
        }
        self.STORE_PATH.write_text(json.dumps(data, indent=2))
    
    def _hash_query(self, query: str) -> str:
        """Create hash for query similarity matching."""
        # Normalize: lowercase, sort words
        normalized = " ".join(sorted(query.lower().split()))
        return hashlib.md5(normalized.encode()).hexdigest()[:12]
    
    def _extract_keywords(self, query: str) -> list[str]:
        """Extract meaningful keywords from query."""
        import re
        stop_words = {'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been',
                      'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would',
                      'for', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'from',
                      'with', 'by', 'this', 'that', 'it', 'we', 'they', 'i', 'you',
                      'what', 'where', 'why', 'how', 'when', 'which', 'who'}
        
        words = re.findall(r'[a-zA-Z_][a-zA-Z0-9_]*', query.lower())
        return [w for w in words if w not in stop_words and len(w) > 2]
    
    def add_feedback(
        self, 
        query: str, 
        predicted: list[str], 
        actual: str
    ) -> None:
        """
        Record user feedback.
        
        Args:
            query: The original search query
            predicted: List of file paths we predicted
            actual: The file path user said was correct
        """
        keywords = self._extract_keywords(query)
        
        feedback = Feedback(
            query=query,
            query_hash=self._hash_query(query),
            predicted=predicted,
            actual=actual,
            timestamp=datetime.now().isoformat(),
            helpful_keywords=keywords
        )
        
        self.feedbacks.append(feedback)
        
        # Learn: associate keywords with the correct file
        for kw in keywords:
            if kw not in self.keyword_boosts:
                self.keyword_boosts[kw] = []
            
            # Store file pattern (last 2 parts of path)
            parts = actual.replace("\\", "/").split("/")
            pattern = "/".join(parts[-2:]) if len(parts) >= 2 else parts[-1]
            
            if pattern not in self.keyword_boosts[kw]:
                self.keyword_boosts[kw].append(pattern)
        
        self._save()
    
    def get_boosts_for_query(self, query: str) -> dict[str, float]:
        """
        Get learned boosts for a query.
        
        Returns:
            Dict of file_pattern → boost_multiplier
        """
        keywords = self._extract_keywords(query)
        boosts: dict[str, float] = {}
        
        for kw in keywords:
            if kw in self.keyword_boosts:
                for pattern in self.keyword_boosts[kw]:
                    # More keyword matches = higher boost
                    boosts[pattern] = boosts.get(pattern, 1.0) + 0.3
        
        return boosts
    
    def find_similar_feedback(self, query: str) -> Optional[Feedback]:
        """Find feedback for a similar past query."""
        query_hash = self._hash_query(query)
        
        for fb in reversed(self.feedbacks):  # Most recent first
            if fb.query_hash == query_hash:
                return fb
        
        return None
    
    def get_stats(self) -> dict:
        """Get feedback statistics."""
        if not self.feedbacks:
            return {"total": 0, "accuracy": 0}
        
        correct = sum(1 for f in self.feedbacks if f.actual in f.predicted)
        
        return {
            "total": len(self.feedbacks),
            "correct_predictions": correct,
            "accuracy": correct / len(self.feedbacks),
            "learned_keywords": len(self.keyword_boosts)
        }
