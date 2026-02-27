from pathlib import Path
from extractors import PythonStackExtractor, JavaStackExtractor, ExtractedError
from indexer import CodeIndexer, CodeEntity
from graph import CallGraph
from retrieval import HybridRetriever
from ranker import LLMRanker


class FaultLocalizer:
    def __init__(self, codebase_path: str, use_llm: bool = True):
        self.codebase_path = Path(codebase_path)
        self.use_llm = use_llm

        self.python_extractor = PythonStackExtractor()
        self.java_extractor = JavaStackExtractor()
        self.indexer = CodeIndexer()
        self.call_graph = CallGraph()
        self.retriever: HybridRetriever | None = None
        self.ranker = LLMRanker() if use_llm else None

        self._indexed = False

    def index(self) -> int:
        """Index the codebase. Returns number of entities indexed."""
        entities = self.indexer.index_directory(self.codebase_path)
        self.call_graph.build(entities)
        self.retriever = HybridRetriever(entities, self.indexer.encoder)
        self._indexed = True
        return len(entities)

    def localize(self, error_text: str, top_k: int = 5) -> list[dict]:
        """
        Localize fault from stack trace.
        
        Returns list of suspected fault locations with explanations.
        """
        if not self._indexed:
            raise RuntimeError("Call index() first")

        # 1. Extract structured info from stack trace
        error = self._extract_error(error_text)

        # 2. Get candidates from stack trace files/methods (direct filter)
        direct_candidates = self._get_direct_candidates(error)

        # 3. Expand via call graph (find potential root causes)
        expanded = self._expand_via_graph(error)

        # 4. Hybrid retrieval on expanded candidate set
        query = f"{error.exception_type} {error.message} {' '.join(error.method_names)}"
        search_results = self.retriever.search(query, top_k=20)

        # 5. Merge and dedupe candidates
        all_candidates = self._merge_candidates(direct_candidates, expanded, search_results)

        # 6. LLM re-rank or return by score
        if self.use_llm and self.ranker:
            return self.ranker.rank_and_explain(error, all_candidates, top_k)
        else:
            return [{"entity": e, "score": s, "reason": ""} for e, s in all_candidates[:top_k]]

    def _extract_error(self, error_text: str) -> ExtractedError:
        if self.python_extractor.can_parse(error_text):
            return self.python_extractor.extract(error_text)
        elif self.java_extractor.can_parse(error_text):
            return self.java_extractor.extract(error_text)
        else:
            # Fallback: treat as unstructured
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
            file_entities = self.indexer.get_entities_by_file(frame.file_path)
            for entity in file_entities:
                if entity.start_line <= frame.line_number <= entity.end_line:
                    candidates.append((entity, 1.0))  # High score for direct match

            # Match by method name
            method_entities = self.indexer.get_entities_by_name(frame.method_name)
            for entity in method_entities:
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
                    expanded.append((entity, 0.7))  # Lower score for indirect
        return expanded

    def _merge_candidates(
        self,
        direct: list[tuple[CodeEntity, float]],
        expanded: list[tuple[CodeEntity, float]],
        searched: list[tuple[CodeEntity, float]]
    ) -> list[tuple[CodeEntity, float]]:
        """Merge and dedupe candidates, keeping highest score per entity."""
        scores: dict[str, tuple[CodeEntity, float]] = {}

        for entity, score in direct + expanded + searched:
            if entity.id not in scores or scores[entity.id][1] < score:
                scores[entity.id] = (entity, score)

        merged = list(scores.values())
        merged.sort(key=lambda x: x[1], reverse=True)
        return merged
