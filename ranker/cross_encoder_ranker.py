"""Cross-encoder reranking for precise relevance scoring.

Uses a lightweight cross-encoder model (ms-marco-MiniLM) as an intermediate
reranking step between retrieval and LLM. The cross-encoder sees both the
query and each candidate together, producing much more accurate relevance
scores than bi-encoder similarity alone.

Pipeline: Retrieval (top 50) → Cross-encoder (top 15) → LLM (top 5)
"""

import logging
from sentence_transformers import CrossEncoder
from indexer.entities import CodeEntity

logger = logging.getLogger(__name__)

# Default model — small, fast, good at relevance ranking
DEFAULT_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"


class CrossEncoderRanker:
    """Rerank candidates using a cross-encoder for precise query-document scoring."""

    def __init__(self, model_name: str = DEFAULT_MODEL):
        try:
            self.model = CrossEncoder(model_name, max_length=512)
            self.available = True
            logger.info(f"Cross-encoder loaded: {model_name}")
        except Exception as e:
            logger.warning(f"Cross-encoder unavailable ({e}), will pass through")
            self.model = None
            self.available = False

    def rerank(
        self,
        query: str,
        candidates: list[tuple[CodeEntity, float]],
        top_k: int = 15,
    ) -> list[tuple[CodeEntity, float]]:
        """
        Rerank candidates using cross-encoder scores.

        The cross-encoder takes (query, document) pairs and produces a single
        relevance score, which is much more accurate than cosine similarity
        from bi-encoders.

        Args:
            query: The search query or error text
            candidates: List of (entity, retrieval_score) tuples
            top_k: Number of candidates to return

        Returns:
            Reranked list of (entity, cross_encoder_score) tuples
        """
        if not self.available or not candidates:
            return candidates[:top_k]

        # Build (query, document) pairs for cross-encoder
        pairs = []
        for entity, _ in candidates:
            doc = self._entity_to_text(entity)
            pairs.append((query, doc))

        try:
            scores = self.model.predict(pairs, show_progress_bar=False)

            # Combine with original retrieval scores (cross-encoder dominant)
            scored = []
            for i, (entity, retrieval_score) in enumerate(candidates):
                # Normalize cross-encoder score to 0-1 range (sigmoid-like)
                ce_score = float(scores[i])
                # Weighted combination: 70% cross-encoder, 30% retrieval
                combined = 0.7 * self._sigmoid(ce_score) + 0.3 * min(1.0, retrieval_score)
                scored.append((entity, combined))

            scored.sort(key=lambda x: x[1], reverse=True)
            return scored[:top_k]

        except Exception as e:
            logger.warning(f"Cross-encoder reranking failed: {e}")
            return candidates[:top_k]

    def _entity_to_text(self, entity: CodeEntity) -> str:
        """Convert entity to text for cross-encoder input (max ~256 tokens)."""
        parts = []
        if entity.file_path:
            parts.append(entity.file_path)
        parts.append(entity.full_name)
        if entity.annotations:
            parts.append(" ".join(entity.annotations[:3]))
        parts.append(entity.signature)
        if entity.docstring:
            parts.append(entity.docstring[:200])
        if entity.body:
            parts.append(entity.body[:300])
        return " ".join(parts)[:512]

    @staticmethod
    def _sigmoid(x: float) -> float:
        """Map raw cross-encoder score to 0-1 range."""
        import math
        try:
            return 1.0 / (1.0 + math.exp(-x))
        except OverflowError:
            return 0.0 if x < 0 else 1.0
