"""Production fault localizer with OpenSearch backend."""

import queue
import threading
import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from sentence_transformers import SentenceTransformer

from extractors import PythonStackExtractor, JavaStackExtractor, ExtractedError, StackFrame, ImageExtractor
from extractors.scalable_ui_mapper import ScalableUIMapper
from indexer import CodeIndexer, CodeEntity
from indexer.python_parser import PythonParser
from indexer.java_parser import JavaParser
from indexer.js_ts_parser import JsTsParser
from indexer.html_parser import HtmlParser
from indexer.relationship_resolver import RelationshipResolver
from storage import OpenSearchStore
from graph import CallGraph
from ranker import LLMRanker
from ranker.graph_ranker import GraphRanker
from ranker.cross_encoder_ranker import CrossEncoderRanker

logger = logging.getLogger(__name__)

# Library path patterns — frames matching these are deprioritized
_LIB_PATTERNS = (
    "site-packages", "dist-packages", "node_modules", "jre/lib", "jdk/",
    "rt.jar", "java.base/", "java.lang.", "java.util.", "sun.",
    "org.springframework.aop", "org.springframework.cglib",
    "org.apache.catalina", "org.apache.coyote", "org.apache.tomcat",
    "werkzeug/", "flask/app", "django/core/handlers", "uvicorn/",
    "starlette/", "fastapi/routing", "gunicorn/",
)

# Sentinel to signal pipeline stage completion
_DONE = object()


class FaultLocalizerProd:
    def __init__(
        self,
        opensearch_host: str = "localhost",
        opensearch_port: int = 9200,
        use_llm: bool = True,
        encoder_model: str = "microsoft/codebert-base",
        use_cross_encoder: bool = True,
    ):
        self.store = OpenSearchStore(host=opensearch_host, port=opensearch_port)
        self.encoder = SentenceTransformer(encoder_model)
        self.python_extractor = PythonStackExtractor()
        self.java_extractor = JavaStackExtractor()
        self.image_extractor = ImageExtractor()
        self.ui_mapper = ScalableUIMapper(self.store.client)
        self.ranker = LLMRanker() if use_llm else None
        self.graph_ranker = GraphRanker(self.store)
        self.cross_encoder = CrossEncoderRanker() if use_cross_encoder else None
        self.use_llm = use_llm

    def index_codebase(self, path: str, workers: int = 4, namespace: str = None, progress_callback=None) -> int:
        """
        4-stage pipeline with real-time progress reporting:
          Stage 1 (Parse):   N threads parse files → entities
          Stage 2 (Resolve): Cross-file call resolution, inheritance, import graph
          Stage 3 (Embed):   Batch-encode with CodeBERT (chunk-level)
          Stage 4 (Index):   Bulk upsert to OpenSearch

        progress_callback(stage, **kwargs) is called with stage-level updates.
        """
        from indexer.background_indexer import CancelledError

        def report(stage, **kwargs):
            if progress_callback:
                try:
                    progress_callback(stage, **kwargs)
                except CancelledError:
                    raise
                except Exception:
                    pass

        codebase = Path(path)
        if not namespace:
            namespace = codebase.name

        skip_dirs = {"venv", "node_modules", ".git", "__pycache__", "build", "dist", "cdk.out"}
        all_files = [
            f for f in codebase.rglob("*")
            if f.suffix in (".py", ".java", ".js", ".ts", ".html")
            and not any(d in f.parts for d in skip_dirs)
        ]

        if not all_files:
            return 0

        indexer = CodeIndexer.__new__(CodeIndexer)
        indexer.python_parser = PythonParser()
        indexer.java_parser = JavaParser()
        indexer.js_ts_parser = JsTsParser()
        indexer.html_parser = HtmlParser()
        resolver = RelationshipResolver()

        # ── Stage 1: Parse (multi-threaded) ──────────────────────────
        report("parsing", files_total=len(all_files), files_parsed=0, entities_parsed=0)
        all_entities = []
        files_done = [0]

        def parse_file(f):
            try:
                if f.suffix == ".py":
                    return indexer.python_parser.parse_file(f)
                elif f.suffix == ".java":
                    return indexer.java_parser.parse_file(f)
                elif f.suffix in (".js", ".ts"):
                    return indexer.js_ts_parser.parse_file(f)
                elif f.suffix == ".html":
                    return indexer.html_parser.parse_file(f)
            except Exception as e:
                logger.debug(f"Parse error {f}: {e}")
            return []

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(parse_file, f): f for f in all_files}
            for future in as_completed(futures):
                try:
                    entities = future.result()
                    for ent in entities:
                        ent.namespace = namespace
                        all_entities.append(ent)
                except Exception as e:
                    logger.debug(f"Parse future error: {e}")
                files_done[0] += 1
                # report() checks cancel event via progress_callback
                if files_done[0] % 10 == 0 or files_done[0] == len(all_files):
                    try:
                        report("parsing", files_total=len(all_files), files_parsed=files_done[0], entities_parsed=len(all_entities))
                    except CancelledError:
                        # Cancel remaining futures and propagate
                        for f in futures:
                            f.cancel()
                        raise

        if not all_entities:
            return 0

        # ── Stage 2: Resolve cross-file relationships ────────────────
        report("resolving", entities_parsed=len(all_entities))
        logger.info(f"Resolving relationships for {len(all_entities)} entities...")
        resolver.resolve(all_entities)
        logger.info("Resolving complete.")

        # ── Stages 3-4: Embed + Index (concurrent via queues) ────────
        report("embedding", entities_embedded=0, entities_total=len(all_entities))
        embed_q = queue.Queue(maxsize=32)
        total_indexed = [0]
        entities_embedded = [0]
        errors = []

        def stage_embed():
            embed_batch_size = 256
            try:
                for i in range(0, len(all_entities), embed_batch_size):
                    chunk = all_entities[i:i + embed_batch_size]
                    self._embed_entities_chunked(chunk)
                    entities_embedded[0] += len(chunk)
                    # report() checks cancel event via progress_callback
                    report("embedding", entities_embedded=entities_embedded[0], entities_total=len(all_entities))
                    embed_q.put(chunk)
            except CancelledError:
                pass
            finally:
                embed_q.put(_DONE)

        def stage_index():
            while True:
                batch = embed_q.get()
                if batch is _DONE:
                    break
                try:
                    self.store.index(batch)
                    for ent in batch:
                        self.ui_mapper.learn_from_entity(ent)
                    total_indexed[0] += len(batch)
                    # report() checks cancel event via progress_callback
                    report("indexing", entities_indexed=total_indexed[0], entities_total=len(all_entities))
                except CancelledError:
                    break
                except Exception as e:
                    errors.append(str(e))
                    logger.error(f"Index error: {e}")

        t_embed = threading.Thread(target=stage_embed, name="embed")
        t_index = threading.Thread(target=stage_index, name="index")

        t_embed.start()
        t_index.start()

        t_embed.join()
        t_index.join()

        if errors:
            logger.warning(f"Indexing completed with {len(errors)} errors")

        return total_indexed[0]

    def localize(self, error_text: str, top_k: int = 5, namespace: str = None) -> list[dict]:
        """Localize fault from stack trace or answer natural language code questions.

        Pipeline:
          1. Extract error / detect NL query
          2. Weighted stack frame scoring (top frame + first app frame boosted)
          3. Hybrid retrieval (BM25 + vector)
          4. Graph-based score propagation (callers/callees of suspicious code)
          5. Cross-encoder reranking (precise query-document scoring)
          6. LLM reranking (final intelligence pass)
        """
        error = self._extract_error(error_text)
        is_nl_query = error.exception_type in ("NLQuery", "Unknown") and not error.frames

        # Auto-detect namespace from stack trace file paths
        if not namespace:
            namespace = self._detect_namespace(error)

        if is_nl_query:
            query = error_text.strip()
        else:
            # Stack frame weighting: build query with weighted frame contributions
            query = self._build_weighted_query(error)

        query_embedding = self.encoder.encode(query).tolist()

        # Direct candidates from stack frames (with position-based weighting)
        direct_candidates = []
        if not is_nl_query:
            direct_candidates = self._weighted_frame_lookup(error, namespace)

        search_results = self.store.search_hybrid(query, query_embedding, top_k=50, namespace=namespace)
        all_candidates = self._merge_candidates(direct_candidates, search_results)

        # Graph-based score propagation
        all_candidates = self.graph_ranker.propagate(all_candidates, namespace=namespace)

        # Cross-encoder reranking (narrows to top 15 with precise scores)
        if self.cross_encoder and self.cross_encoder.available:
            all_candidates = self.cross_encoder.rerank(query, all_candidates, top_k=15)

        if self.use_llm and self.ranker:
            return self.ranker.rank_and_explain(error, all_candidates, top_k)
        else:
            return [{"entity": e, "score": s, "reason": ""} for e, s in all_candidates[:top_k]]

    def localize_from_image(self, image_path: str, top_k: int = 5, context: str = "", namespace: str = None) -> list[dict]:
        """Localize fault from a screenshot."""
        # 1. Extract context from image using vision LLM
        extracted = self.image_extractor.extract_from_image(image_path, context=context)

        # Auto-detect namespace from image extraction if not provided
        if not namespace:
            namespace = self._detect_namespace_from_image(extracted)

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
            entities = self.store.get_by_name(pattern, namespace=namespace)
            for entity in entities:
                candidates.append((entity, 0.9))

        # Hybrid search for broader results
        search_results = self.store.search_hybrid(query, query_embedding, top_k=50, namespace=namespace)
        candidates.extend(search_results)

        # Dedupe and sort
        all_candidates = self._merge_candidates(candidates, [])

        # Graph-based score propagation
        all_candidates = self.graph_ranker.propagate(all_candidates, namespace=namespace)

        # Cross-encoder reranking
        if self.cross_encoder and self.cross_encoder.available:
            all_candidates = self.cross_encoder.rerank(query, all_candidates, top_k=15)

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
    def localize_unified(self, error_text: str = "", image_path: str = "", top_k: int = 5, namespace: str = None) -> dict:
        """Unified localization: text + optional image. Returns results and image extraction data."""
        image_extracted = None
        image_query_parts = []

        # If image provided, extract context from it
        if image_path:
            image_extracted = self.image_extractor.extract_from_image(image_path, context=error_text)
            search_context = self.ui_mapper.build_search_context(image_extracted)
            image_query_parts.extend(search_context["code_patterns"])
            if search_context["error_text"]:
                image_query_parts.append(search_context["error_text"])
            if search_context["context"]:
                image_query_parts.append(search_context["context"])
            # Auto-detect namespace from image if not provided
            if not namespace:
                namespace = self._detect_namespace_from_image(image_extracted)

        # Auto-detect namespace from text if still not set
        if not namespace and error_text:
            error = self._extract_error(error_text)
            namespace = self._detect_namespace(error)

        # Build combined query
        all_candidates = []

        # Text-based search
        if error_text:
            error = self._extract_error(error_text)
            is_nl_query = error.exception_type in ("NLQuery", "Unknown") and not error.frames

            if is_nl_query:
                text_query = error_text.strip()
            else:
                text_query = f"{error.exception_type} {error.message} {' '.join(error.method_names)}"

            text_embedding = self.encoder.encode(text_query).tolist()

            if not is_nl_query:
                for frame in error.frames:
                    for entity in self.store.get_by_name(frame.method_name, namespace=namespace):
                        all_candidates.append((entity, 1.0))

            all_candidates.extend(self.store.search_hybrid(text_query, text_embedding, top_k=50, namespace=namespace))

        # Image-based search
        if image_query_parts:
            img_query = " ".join(image_query_parts)
            img_embedding = self.encoder.encode(img_query).tolist()

            for pattern in image_query_parts[:20]:
                for entity in self.store.get_by_name(pattern, namespace=namespace):
                    all_candidates.append((entity, 0.9))

            all_candidates.extend(self.store.search_hybrid(img_query, img_embedding, top_k=50, namespace=namespace))

        merged = self._merge_candidates(all_candidates, [])

        # Graph-based score propagation
        merged = self.graph_ranker.propagate(merged, namespace=namespace)

        # Cross-encoder reranking
        combined_query = " ".join([error_text[:300]] + image_query_parts[:5]) if error_text else " ".join(image_query_parts[:5])
        if self.cross_encoder and self.cross_encoder.available and merged:
            merged = self.cross_encoder.rerank(combined_query, merged, top_k=15)

        # LLM re-rank
        if self.use_llm and self.ranker and merged:
            context_parts = []
            if error_text:
                context_parts.append(error_text[:500])
            if image_extracted:
                context_parts.append(f"UI: {image_extracted.get('app_section', '')} | {image_extracted.get('error_message', '')} | Elements: {image_extracted.get('ui_elements', [])}")

            # Determine query type for LLM prompt selection
            has_stack_trace = error_text and self._extract_error(error_text).frames
            pseudo_error = ExtractedError(
                exception_type="Fault" if has_stack_trace else ("UI Bug" if image_extracted and not error_text else "NLQuery"),
                message=" | ".join(context_parts)[:300],
                frames=self._extract_error(error_text).frames if has_stack_trace else [],
                raw_text="\n".join(context_parts)
            )
            results = self.ranker.rank_and_explain(pseudo_error, merged, top_k)
        else:
            results = [{"entity": e, "score": s, "reason": ""} for e, s in merged[:top_k]]

        return {"results": results, "image_extracted": image_extracted, "namespace_used": namespace, "namespace_source": "auto-detected" if namespace else "none"}

    # ── Stack frame weighting helpers ────────────────────────────

    def _is_library_frame(self, frame) -> bool:
        """Check if a stack frame is from a library (not application code)."""
        path = (frame.file_path or "").replace("\\", "/")
        pkg = frame.package or ""
        combined = f"{path} {pkg}".lower()
        return any(p in combined for p in _LIB_PATTERNS)

    def _build_weighted_query(self, error: ExtractedError) -> str:
        """Build search query with stack frame weighting.

        Strategy:
        - Top frame (index 0) gets 3x weight (most immediate symptom)
        - First application frame gets 3x weight (most likely root cause)
        - Other app frames get 1x weight
        - Library frames are excluded from query
        """
        parts = [error.exception_type, error.message]

        if not error.frames:
            return " ".join(parts)

        first_app_frame = None
        for frame in error.frames:
            if not self._is_library_frame(frame):
                first_app_frame = frame
                break

        for i, frame in enumerate(error.frames):
            if self._is_library_frame(frame):
                continue

            method = frame.full_method
            repeat = 1

            # Top frame gets 3x
            if i == 0:
                repeat = 3
            # First application frame gets 3x (if different from top)
            elif frame == first_app_frame:
                repeat = 3

            for _ in range(repeat):
                parts.append(method)

        return " ".join(parts)

    def _weighted_frame_lookup(
        self, error: ExtractedError, namespace: str | None
    ) -> list[tuple[CodeEntity, float]]:
        """Look up entities for stack frames with position-based scoring.

        Top frame and first app frame get score 1.0.
        Other app frames decay: 0.8, 0.6, 0.4, ...
        Library frames get 0.2.
        """
        candidates = []
        if not error.frames:
            return candidates

        first_app_idx = None
        for i, frame in enumerate(error.frames):
            if not self._is_library_frame(frame):
                first_app_idx = i
                break

        app_rank = 0
        for i, frame in enumerate(error.frames):
            is_lib = self._is_library_frame(frame)

            if i == 0:
                score = 1.0
            elif i == first_app_idx:
                score = 1.0
            elif is_lib:
                score = 0.2
            else:
                score = max(0.3, 1.0 - app_rank * 0.2)
                app_rank += 1

            entities = self.store.get_by_name(frame.method_name, namespace=namespace)
            for entity in entities:
                candidates.append((entity, score))

        return candidates

    # ── Chunk-level embedding helper ─────────────────────────────

    def _embed_entities_chunked(self, entities: list) -> None:
        """Embed entities using chunk-level embeddings.

        Skips trivial entities (< 30 chars body). For entities > 512 chars,
        splits into overlapping chunks, embeds each, and averages.
        """
        import numpy as np

        single_entities = []
        chunked_entities = []
        trivial_count = 0

        for ent in entities:
            # Skip trivial entities — getters, setters, tiny stubs
            # But always embed fields/enums (constants are short but context-rich)
            body_len = len(ent.body) if ent.body else 0
            if body_len < 30 and ent.entity_type.value not in ("field", "enum"):
                ent.embedding = [0.0] * 768
                trivial_count += 1
                continue

            chunks = ent.to_embedding_chunks(chunk_size=512, overlap=64)
            if len(chunks) <= 1:
                single_entities.append((ent, chunks[0] if chunks else ent.to_embedding_text()))
            else:
                chunked_entities.append((ent, chunks))

        # Batch encode single-chunk entities
        if single_entities:
            texts = [t for _, t in single_entities]
            embeddings = self.encoder.encode(texts, show_progress_bar=False, batch_size=256)
            for (ent, _), emb in zip(single_entities, embeddings):
                ent.embedding = emb.tolist()

        # Encode chunked entities: all chunks in one batch, then average per entity
        if chunked_entities:
            all_chunks = []
            chunk_map = []
            for i, (ent, chunks) in enumerate(chunked_entities):
                all_chunks.extend(chunks)
                chunk_map.append((i, len(chunks)))

            all_embeddings = self.encoder.encode(all_chunks, show_progress_bar=False, batch_size=256)

            offset = 0
            for ent_idx, count in chunk_map:
                ent = chunked_entities[ent_idx][0]
                chunk_embs = all_embeddings[offset:offset + count]
                mean_emb = np.mean(chunk_embs, axis=0)
                ent.embedding = mean_emb.tolist()
                offset += count

    def _detect_namespace(self, error) -> str | None:
        """Auto-detect namespace from stack trace file paths by matching against indexed namespaces."""
        try:
            namespaces = self.store.list_namespaces()
            ns_names = [n["namespace"] for n in namespaces]
            if not ns_names:
                return None

            # Check file paths in stack frames
            for frame in error.frames:
                path = frame.file_path.lower() if frame.file_path else ""
                for ns in ns_names:
                    if ns.lower() in path:
                        return ns

            # Check package names
            for frame in error.frames:
                pkg = (frame.package or "").lower()
                for ns in ns_names:
                    if ns.lower() in pkg:
                        return ns

            # Check error message text
            raw = error.raw_text.lower() if error.raw_text else ""
            for ns in ns_names:
                if ns.lower() in raw:
                    return ns
        except Exception:
            pass
        return None

    def _detect_namespace_from_image(self, extracted: dict) -> str | None:
        """Auto-detect namespace from image extraction (app name, section, keywords)."""
        try:
            namespaces = self.store.list_namespaces()
            ns_names = [n["namespace"] for n in namespaces]
            if not ns_names:
                return None

            # Check app_section, keywords, raw_text from image extraction
            search_fields = [
                extracted.get("app_section", ""),
                extracted.get("raw_text", ""),
                " ".join(extracted.get("keywords", [])),
                " ".join(extracted.get("ui_elements", [])),
            ]
            combined = " ".join(search_fields).lower()

            for ns in ns_names:
                if ns.lower() in combined:
                    return ns
        except Exception:
            pass
        return None

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
            # Dedupe by id, and also by name+signature to catch copies in different paths
            key = entity.id
            sig_key = f"{entity.name}:{entity.signature}:{entity.start_line}"
            existing_by_sig = None
            for k, (e, s) in scores.items():
                if f"{e.name}:{e.signature}:{e.start_line}" == sig_key:
                    existing_by_sig = k
                    break
            dedup_key = existing_by_sig or key
            if dedup_key not in scores or scores[dedup_key][1] < score:
                scores[dedup_key] = (entity, score)
        merged = list(scores.values())
        merged.sort(key=lambda x: x[1], reverse=True)
        return merged
