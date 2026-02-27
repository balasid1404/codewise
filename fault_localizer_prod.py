"""Production fault localizer with OpenSearch backend."""

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from sentence_transformers import SentenceTransformer

from extractors import PythonStackExtractor, JavaStackExtractor, ExtractedError, ImageExtractor
from extractors.scalable_ui_mapper import ScalableUIMapper
from indexer import CodeIndexer, CodeEntity
from storage import OpenSearchStore
from graph import CallGraph
from ranker import LLMRanker


class FaultLocalizerProd:
    def __init__(
        self,
        opensearch_host: str = "localhost",
        opensearch_port: int = 9200,
        use_llm: bool = True,
        encoder_model: str = "microsoft/codebert-base"
    ):
        self.store = OpenSearchStore(host=opensearch_host, port=opensearch_port)
        self.encoder = SentenceTransformer(encoder_model)
        self.python_extractor = PythonStackExtractor()
        self.java_extractor = JavaStackExtractor()
        self.image_extractor = ImageExtractor()
        self.ui_mapper = ScalableUIMapper(self.store.client)  # Uses OpenSearch
        self.ranker = LLMRanker() if use_llm else None
        self.use_llm = use_llm

    def index_codebase(self, path: str, workers: int = 4) -> int:
        """Index codebase with parallel workers."""
        codebase = Path(path)
        indexer = CodeIndexer(model_name="microsoft/codebert-base")

        py_files = list(codebase.rglob("*.py"))
        java_files = list(codebase.rglob("*.java"))
        all_files = py_files + java_files

        skip_dirs = {"venv", "node_modules", ".git", "__pycache__", "build", "dist"}
        all_files = [f for f in all_files if not any(d in f.parts for d in skip_dirs)]

        total_indexed = 0
        batch_size = max(1, len(all_files) // workers)

        def process_batch(files):
            entities = []
            for f in files:
                try:
                    if f.suffix == ".py":
                        entities.extend(indexer.python_parser.parse_file(f))
                    else:
                        entities.extend(indexer.java_parser.parse_file(f))
                except Exception:
                    continue

            if entities:
                texts = [e.to_search_text() for e in entities]
                embeddings = self.encoder.encode(texts, show_progress_bar=False)
                for entity, emb in zip(entities, embeddings):
                    entity.embedding = emb.tolist()

            return entities

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = []
            for i in range(0, len(all_files), batch_size):
                batch = all_files[i:i + batch_size]
                futures.append(executor.submit(process_batch, batch))

            for future in as_completed(futures):
                entities = future.result()
                if entities:
                    self.store.index(entities)
                    # Learn vocabulary in OpenSearch
                    for entity in entities:
                        self.ui_mapper.learn_from_entity(entity)
                    total_indexed += len(entities)

        return total_indexed

    def localize(self, error_text: str, top_k: int = 5) -> list[dict]:
        """Localize fault from stack trace."""
        error = self._extract_error(error_text)

        query = f"{error.exception_type} {error.message} {' '.join(error.method_names)}"
        query_embedding = self.encoder.encode(query).tolist()

        direct_candidates = []
        for frame in error.frames:
            entities = self.store.get_by_name(frame.method_name)
            for entity in entities:
                direct_candidates.append((entity, 1.0))

        search_results = self.store.search_hybrid(query, query_embedding, top_k=50)
        all_candidates = self._merge_candidates(direct_candidates, search_results)

        if self.use_llm and self.ranker:
            return self.ranker.rank_and_explain(error, all_candidates, top_k)
        else:
            return [{"entity": e, "score": s, "reason": ""} for e, s in all_candidates[:top_k]]

    def localize_from_image(self, image_path: str, top_k: int = 5) -> list[dict]:
        """Localize fault from a screenshot."""
        # 1. Extract context from image using vision LLM
        extracted = self.image_extractor.extract_from_image(image_path)

        # 2. Map UI elements to code patterns
        search_context = self.ui_mapper.build_search_context(extracted)

        # 3. Build search query
        query_parts = []
        query_parts.extend(search_context["code_patterns"])
        if search_context["error_text"]:
            query_parts.append(search_context["error_text"])
        if search_context["context"]:
            query_parts.append(search_context["context"])

        query = " ".join(query_parts)
        query_embedding = self.encoder.encode(query).tolist()

        # 4. Search with code patterns
        candidates = []

        # Search by code patterns (high priority)
        for pattern in search_context["code_patterns"][:20]:
            entities = self.store.get_by_name(pattern)
            for entity in entities:
                candidates.append((entity, 0.9))

        # Hybrid search for broader results
        search_results = self.store.search_hybrid(query, query_embedding, top_k=50)
        candidates.extend(search_results)

        # Dedupe and sort
        all_candidates = self._merge_candidates(candidates, [])

        # 5. LLM re-rank with image context
        if self.use_llm and self.ranker:
            # Create pseudo-error for ranker
            pseudo_error = ExtractedError(
                exception_type="UI Bug",
                message=f"{extracted.get('app_section', 'unknown')}: {extracted.get('error_message', 'visual bug')}",
                frames=[],
                raw_text=f"User action: {extracted.get('user_action', 'unknown')}\nUI elements: {extracted.get('ui_elements', [])}\nError: {extracted.get('error_message', 'none')}"
            )
            return self.ranker.rank_and_explain(pseudo_error, all_candidates, top_k)
        else:
            return [{
                "entity": e,
                "score": s,
                "reason": "",
                "image_context": extracted
            } for e, s in all_candidates[:top_k]]

    def _extract_error(self, error_text: str) -> ExtractedError:
        if self.python_extractor.can_parse(error_text):
            return self.python_extractor.extract(error_text)
        elif self.java_extractor.can_parse(error_text):
            return self.java_extractor.extract(error_text)
        else:
            return ExtractedError(
                exception_type="Unknown",
                message=error_text[:200],
                frames=[],
                raw_text=error_text
            )

    def _merge_candidates(
        self,
        direct: list[tuple[CodeEntity, float]],
        searched: list[tuple[CodeEntity, float]]
    ) -> list[tuple[CodeEntity, float]]:
        scores: dict[str, tuple[CodeEntity, float]] = {}
        for entity, score in direct + searched:
            if entity.id not in scores or scores[entity.id][1] < score:
                scores[entity.id] = (entity, score)
        merged = list(scores.values())
        merged.sort(key=lambda x: x[1], reverse=True)
        return merged
