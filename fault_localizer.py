"""
Core fault localization engine.

Pipeline:
1. Extract structured info from error (stack trace, image, or text)
2. Find direct matches from stack frames
3. Expand via call graph to find root causes
4. Hybrid search (BM25 + semantic) for additional candidates
5. LLM re-ranking with explanations
"""

from pathlib import Path

from extractors import PythonStackExtractor, JavaStackExtractor, ExtractedError
from indexer import CodeIndexer, CodeEntity
from graph import CallGraph
from retrieval import HybridRetriever
from ranker import LLMRanker


class FaultLocalizer:
    """Main fault localization engine."""

    def __init__(self, codebase_path: str, use_llm: bool = True):
        self.codebase_path = Path(codebase_path)
        self.use_llm = use_llm

        # Components
        self.python_extractor = PythonStackExtractor()
        self.java_extractor = JavaStackExtractor()
        self.indexer = CodeIndexer()
        self.call_graph = CallGraph()
        self.retriever: HybridRetriever | None = None
        self.ranker = LLMRanker() if use_llm else None

        self._indexed = False

    def index(self) -> int:
        """Index the codebase. Returns entity count."""
        entities = self.indexer.index_directory(self.codebase_path)
        self.call_graph.build(entities)
        self.retriever = HybridRetriever(entities, self.indexer.encoder)
        self._indexed = True
        return len(entities)

    def load_from_cache(self, cached_data: dict) -> None:
        """Load indexed data from cache."""
        self.indexer.entities = cached_data["entities"]
        self._indexed = True
        
        entities = list(cached_data["entities"].values())
        self.retriever = HybridRetriever(entities, self.indexer.encoder)
        self.call_graph.build(entities)

    def get_cache_data(self) -> dict:
        """Get data for caching."""
        return {
            "count": len(self.indexer.entities),
            "entities": self.indexer.entities
        }

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """
        Search codebase with natural language query.
        
        Examples:
            "where do we handle payment validation?"
            "code that sets subscription prices"
        """
        if not self._indexed:
            raise RuntimeError("Call index() first")

        results = self.retriever.search(query, top_k=top_k)
        return [{"entity": e, "score": s, "reason": ""} for e, s in results]

    def localize(self, error_text: str, top_k: int = 5) -> list[dict]:
        """
        Localize fault from stack trace or error message.
        
        Returns ranked list of suspected fault locations with explanations.
        """
        if not self._indexed:
            raise RuntimeError("Call index() first")

        # 1. Extract structured info
        error = self._extract_error(error_text)

        # 2. Direct matches from stack frames
        direct = self._get_direct_candidates(error)

        # 3. Expand via call graph (find root causes)
        expanded = self._expand_via_graph(error)

        # 4. Hybrid search
        query = f"{error.exception_type} {error.message} {' '.join(error.method_names)}"
        searched = self.retriever.search(query, top_k=20)

        # 5. Merge candidates
        all_candidates = self._merge_candidates(direct, expanded, searched)

        # 6. LLM re-rank or return by score
        if self.use_llm and self.ranker:
            return self.ranker.rank_and_explain(error, all_candidates, top_k)
        
        return [{"entity": e, "score": s, "reason": ""} for e, s in all_candidates[:top_k]]

    def _extract_error(self, error_text: str) -> ExtractedError:
        """Parse error text into structured format."""
        if self.python_extractor.can_parse(error_text):
            return self.python_extractor.extract(error_text)
        if self.java_extractor.can_parse(error_text):
            return self.java_extractor.extract(error_text)
        
        # Fallback: treat as unstructured text
        return ExtractedError(
            exception_type="Unknown",
            message=error_text[:200],
            frames=[],
            raw_text=error_text
        )

    def _get_direct_candidates(self, error: ExtractedError) -> list[tuple[CodeEntity, float]]:
        """Get entities directly mentioned in stack trace."""
        candidates = []
        
        for frame in error.frames:
            # Match by file path
            for entity in self.indexer.get_entities_by_file(frame.file_path):
                if entity.start_line <= frame.line_number <= entity.end_line:
                    candidates.append((entity, 1.0))

            # Match by method name
            for entity in self.indexer.get_entities_by_name(frame.method_name):
                if (entity, 1.0) not in candidates:
                    candidates.append((entity, 0.9))

        return candidates

    def _expand_via_graph(self, error: ExtractedError) -> list[tuple[CodeEntity, float]]:
        """Find callers of stack trace methods (potential root causes)."""
        expanded = []
        
        for frame in error.frames:
            callers = self.call_graph.get_callers(frame.method_name, depth=2)
            for caller_name in callers:
                entities = self.indexer.get_entities_by_name(caller_name.split(".")[-1])
                for entity in entities:
                    expanded.append((entity, 0.7))
        
        return expanded

    def _merge_candidates(
        self,
        direct: list[tuple[CodeEntity, float]],
        expanded: list[tuple[CodeEntity, float]],
        searched: list[tuple[CodeEntity, float]]
    ) -> list[tuple[CodeEntity, float]]:
        """Merge and dedupe candidates, keeping highest score."""
        scores: dict[str, tuple[CodeEntity, float]] = {}

        for entity, score in direct + expanded + searched:
            if entity.id not in scores or scores[entity.id][1] < score:
                scores[entity.id] = (entity, score)

        merged = sorted(scores.values(), key=lambda x: x[1], reverse=True)
        return merged
