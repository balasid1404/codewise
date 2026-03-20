"""
Core fault localization engine.

Pipeline:
1. Extract structured info from error (stack trace, image, or text)
2. Find direct matches from stack frames
2b. Detect rename/refactor intent → exhaustive reference mode
3. Expand via call graph to find root causes
4. Hybrid search (BM25 + semantic) for additional candidates
5. LLM re-ranking with explanations
"""

import re
from pathlib import Path

from extractors import PythonStackExtractor, JavaStackExtractor, ExtractedError
from indexer import CodeIndexer, CodeEntity
from graph import CallGraph
from retrieval import HybridRetriever
from ranker import LLMRanker

# Rename/refactor intent keywords
_RENAME_KEYWORDS = {
    "rename", "renaming", "refactor", "refactoring", "replace", "replacing",
    "update", "updating", "change", "changing", "migrate", "migrating",
    "move", "moving", "deprecate", "deprecating",
}

# Reference/usage search keywords — triggers exhaustive mode even without rename intent
_REFERENCE_KEYWORDS = {
    "find", "search", "locate", "where", "which", "usage", "usages",
    "reference", "references", "referenced", "uses", "used", "using",
    "occurrences", "instances", "affected", "impacts", "depends",
    "callers", "consumers", "dependents", "files",
}

_RENAME_PATTERNS = [
    re.compile(r'\brename\b.*\bto\b', re.IGNORECASE),
    re.compile(r'\bchange\b.*\bto\b', re.IGNORECASE),
    re.compile(r'\breplace\b.*\bwith\b', re.IGNORECASE),
    re.compile(r'\bupdate\b.*\bto\b', re.IGNORECASE),
    re.compile(r'\bmigrate\b.*\bto\b', re.IGNORECASE),
    re.compile(r'\b(\w+)\s*(?:→|->|=>)\s*(\w+)', re.IGNORECASE),
    re.compile(r'\b(?:find|search|locate)\b.*\b(?:usage|reference|occurrence)', re.IGNORECASE),
    re.compile(r'\bwhere\b.*\b(?:used|referenced|called|defined)', re.IGNORECASE),
    re.compile(r'\b(?:files?|classes?|methods?)\b.*\b(?:that|which)\b.*\b(?:use|reference|contain|import)', re.IGNORECASE),
    re.compile(r'\b(?:all|every)\b.*\b(?:usage|reference|occurrence|place)', re.IGNORECASE),
]


class FaultLocalizer:
    """Main fault localization engine."""

    def __init__(self, codebase_path: str):
        self.codebase_path = Path(codebase_path)

        # Components
        self.python_extractor = PythonStackExtractor()
        self.java_extractor = JavaStackExtractor()
        self.indexer = CodeIndexer()
        self.call_graph = CallGraph()
        self.retriever: HybridRetriever | None = None
        self.ranker = LLMRanker()

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
        return [{"entity": e, "score": s, "confidence": s, "reason": ""} for e, s in results]

    def localize(self, error_text: str, top_k: int = 5) -> list[dict]:
        """
        Localize fault from stack trace or error message.
        
        Returns ranked list of suspected fault locations with explanations.
        """
        if not self._indexed:
            raise RuntimeError("Call index() first")

        # 1. Extract structured info
        error = self._extract_error(error_text)
        is_nl_query = error.exception_type in ("NLQuery", "Unknown") and not error.frames

        # 1b. Detect rename/refactor intent for NL queries
        if is_nl_query:
            identifiers = self._extract_identifiers(error_text)
            if self._detect_rename_intent(error_text) and identifiers:
                return self._localize_rename(error_text, error, identifiers, top_k)

        # 2. Direct matches from stack frames
        direct = self._get_direct_candidates(error)

        # 2b. For NL queries, extract identifiers and do exact-match lookups
        if is_nl_query:
            identifiers = self._extract_identifiers(error_text)
            for ident in identifiers:
                for entity in self.indexer.get_entities_by_name(ident):
                    direct.append((entity, 1.0))

        # 3. Expand via call graph (find root causes)
        expanded = self._expand_via_graph(error)

        # 4. Hybrid search
        if is_nl_query:
            query = error_text.strip()
        else:
            query = f"{error.exception_type} {error.message} {' '.join(error.method_names)}"
        searched = self.retriever.search(query, top_k=20)

        # 5. Merge candidates (entity-level dedup)
        all_candidates = self._merge_candidates(direct, expanded, searched)

        # 5b. File-level dedup before LLM — so the LLM ranks across unique files
        file_deduped = self._dedupe_by_file(all_candidates)

        # 6. LLM re-rank
        return self.ranker.rank_and_explain(error, file_deduped, top_k)

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
        """Merge and dedupe candidates, keeping highest score per entity ID."""
        scores: dict[str, tuple[CodeEntity, float]] = {}

        for entity, score in direct + expanded + searched:
            if entity.id not in scores or scores[entity.id][1] < score:
                scores[entity.id] = (entity, score)

        merged = sorted(scores.values(), key=lambda x: x[1], reverse=True)
        return merged

    @staticmethod
    def _dedupe_by_file(
        candidates: list[tuple[CodeEntity, float]]
    ) -> list[tuple[CodeEntity, float]]:
        """Keep only the highest-scoring entity per file path."""
        best: dict[str, tuple[CodeEntity, float]] = {}
        for entity, score in candidates:
            if entity.file_path not in best or score > best[entity.file_path][1]:
                best[entity.file_path] = (entity, score)
        return sorted(best.values(), key=lambda x: x[1], reverse=True)

    # ── Rename/refactor detection and exhaustive search ──────────

    def _detect_rename_intent(self, text: str) -> bool:
        """Detect if the query is a rename/refactor/reference-finding task."""
        text_lower = text.lower()
        words = set(re.findall(r'[a-z]+', text_lower))

        identifiers = self._extract_identifiers(text)
        if not identifiers:
            return False

        if words & _RENAME_KEYWORDS:
            return True
        if words & _REFERENCE_KEYWORDS:
            return True
        for pattern in _RENAME_PATTERNS:
            if pattern.search(text):
                return True
        return False

    def _extract_identifiers(self, text: str) -> list[str]:
        """Extract code identifier-like tokens from natural language text."""
        identifiers = set()

        # UPPER_SNAKE_CASE
        for m in re.finditer(r'\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b', text):
            identifiers.add(m.group(1))
        # CamelCase
        for m in re.finditer(r'\b([A-Z][a-z]+(?:[A-Z][a-z0-9]*)+)\b', text):
            identifiers.add(m.group(1))
        # camelCase methods
        for m in re.finditer(r'\b([a-z][a-z0-9]*(?:[A-Z][a-z0-9]*)+)\b', text):
            if len(m.group(1)) > 6:
                identifiers.add(m.group(1))
        # Quoted identifiers
        for m in re.finditer(r'["\']([A-Za-z_][A-Za-z0-9_]+)["\']', text):
            identifiers.add(m.group(1))
        # Dotted qualified names
        for m in re.finditer(r'\b([a-z][a-z0-9]*(?:\.[a-z][a-z0-9]*){2,})\b', text):
            identifiers.add(m.group(1))

        return sorted(identifiers, key=len, reverse=True)[:20]

    def _localize_rename(
        self, error_text: str, error: ExtractedError,
        identifiers: list[str], top_k: int
    ) -> list[dict]:
        """Exhaustive in-memory reference search for rename/refactor queries.
        
        For pure reference searches (find all usages), skips LLM ranking and
        returns all matching files directly. For rename/refactor queries,
        uses LLM only on file-deduped candidates.
        """
        all_candidates: list[tuple[CodeEntity, float]] = []
        all_entities = list(self.indexer.entities.values())

        for ident in identifiers:
            # 1. Definition lookup (exact name match)
            for entity in self.indexer.get_entities_by_name(ident):
                all_candidates.append((entity, 1.0))

            # 2. Reference field search
            for entity in all_entities:
                if entity.references and ident in entity.references:
                    all_candidates.append((entity, 0.9))

            # 3. Body search — literal string match in body
            for entity in all_entities:
                if entity.body and ident in entity.body:
                    if not any(e.id == entity.id for e, _ in all_candidates):
                        all_candidates.append((entity, 0.95))

        # 4. Entity-level dedup, then file-level dedup
        merged = self._merge_candidates(all_candidates, [], [])
        file_deduped = self._dedupe_by_file(merged)

        # 5. Detect if this is a pure reference search (no rename target)
        is_pure_reference = self._is_pure_reference_search(error_text)

        if is_pure_reference:
            # Skip LLM — return all matching files sorted by score
            return [
                {"entity": e, "score": s, "confidence": s, "reason": f"References identifier in this file"}
                for e, s in file_deduped if s >= 0.5
            ]

        # 6. For rename/refactor, use LLM on file-deduped candidates
        effective_top_k = max(top_k, len([e for e, s in file_deduped if s >= 0.5]))

        return self.ranker.rank_and_explain(error, file_deduped, effective_top_k)

    def _is_pure_reference_search(self, text: str) -> bool:
        """Check if query is a pure reference/usage search (not a rename/refactor)."""
        text_lower = text.lower()
        words = set(re.findall(r'[a-z]+', text_lower))
        # If it has rename-specific keywords, it's not a pure reference search
        if words & _RENAME_KEYWORDS:
            return False
        # If it only has reference keywords, it's a pure reference search
        if words & _REFERENCE_KEYWORDS:
            return True
        return False
