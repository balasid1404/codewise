import numpy as np
from rank_bm25 import BM25Okapi
from sentence_transformers import SentenceTransformer
from indexer.entities import CodeEntity


class HybridRetriever:
    def __init__(self, entities: list[CodeEntity], encoder: SentenceTransformer):
        self.entities = entities
        self.encoder = encoder

        # Build BM25 index
        tokenized = [e.to_search_text().lower().split() for e in entities]
        self.bm25 = BM25Okapi(tokenized)

        # Build embedding matrix
        self.embeddings = np.array([e.embedding for e in entities if e.embedding])

    def search(self, query: str, top_k: int = 20, bm25_candidates: int = 100) -> list[tuple[CodeEntity, float]]:
        """Hybrid search: BM25 first pass, then dense re-ranking."""
        # BM25 first pass
        tokenized_query = query.lower().split()
        bm25_scores = self.bm25.get_scores(tokenized_query)
        top_bm25_indices = np.argsort(bm25_scores)[-bm25_candidates:][::-1]

        # Dense re-ranking on BM25 candidates
        query_embedding = self.encoder.encode(query)
        candidates = [(i, self.entities[i]) for i in top_bm25_indices]

        scored = []
        for idx, entity in candidates:
            if entity.embedding:
                dense_score = self._cosine_similarity(query_embedding, entity.embedding)
                bm25_score = bm25_scores[idx]
                # Combine scores (weighted)
                combined = 0.4 * self._normalize(bm25_score, bm25_scores) + 0.6 * dense_score
                scored.append((entity, combined))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def search_by_methods(self, method_names: list[str], top_k: int = 20) -> list[tuple[CodeEntity, float]]:
        """Search for entities matching method names from stack trace."""
        query = " ".join(method_names)
        return self.search(query, top_k)

    def _cosine_similarity(self, a: np.ndarray, b: list[float]) -> float:
        b = np.array(b)
        return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-8)

    def _normalize(self, score: float, all_scores: np.ndarray) -> float:
        min_s, max_s = all_scores.min(), all_scores.max()
        if max_s == min_s:
            return 0.5
        return (score - min_s) / (max_s - min_s)
