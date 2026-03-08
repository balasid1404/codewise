"""Production fault localizer with OpenSearch backend."""

import queue
import threading
import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from sentence_transformers import SentenceTransformer

from extractors import PythonStackExtractor, JavaStackExtractor, ExtractedError, ImageExtractor
from extractors.scalable_ui_mapper import ScalableUIMapper
from indexer import CodeIndexer, CodeEntity
from storage import OpenSearchStore
from graph import CallGraph
from ranker import LLMRanker

logger = logging.getLogger(__name__)

# Sentinel to signal pipeline stage completion
_DONE = object()


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
        self.ui_mapper = ScalableUIMapper(self.store.client)
        self.ranker = LLMRanker() if use_llm else None
        self.use_llm = use_llm

    def index_codebase(self, path: str, workers: int = 4, namespace: str = None) -> int:
        """
        3-stage parallel pipeline:
          Stage 1 (Parse):  N threads parse files → entities (CPU-bound per file, parallelizable)
          Stage 2 (Embed):  Batch-encode entities with CodeBERT (GPU/CPU, sequential batches)
          Stage 3 (Index):  Bulk upsert to OpenSearch (I/O-bound)

        Stages are connected by queues and run concurrently so parsing, embedding,
        and indexing overlap in time.
        """
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

        indexer = CodeIndexer(model_name="microsoft/codebert-base")

        # Queues between stages (bounded to limit memory)
        parse_q = queue.Queue(maxsize=64)   # Stage 1 → Stage 2: batches of entities
        index_q = queue.Queue(maxsize=32)   # Stage 2 → Stage 3: batches with embeddings

        total_indexed = [0]  # mutable counter for threads
        errors = []

        # ── Stage 1: Parse (multi-threaded) ──────────────────────────
        def stage_parse():
            parse_batch_size = 50  # entities per batch sent to stage 2
            buffer = []

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
                            buffer.append(ent)
                            if len(buffer) >= parse_batch_size:
                                parse_q.put(buffer)
                                buffer = []
                    except Exception as e:
                        logger.debug(f"Parse future error: {e}")

            if buffer:
                parse_q.put(buffer)
            parse_q.put(_DONE)

        # ── Stage 2: Embed (batched, sequential for GPU efficiency) ──
        def stage_embed():
            embed_batch_size = 256  # encode this many at once for throughput
            pending = []

            while True:
                batch = parse_q.get()
                if batch is _DONE:
                    break
                pending.extend(batch)

                # Encode when we have enough, or drain remaining
                while len(pending) >= embed_batch_size:
                    chunk = pending[:embed_batch_size]
                    pending = pending[embed_batch_size:]
                    texts = [e.to_search_text() for e in chunk]
                    embeddings = self.encoder.encode(texts, show_progress_bar=False, batch_size=embed_batch_size)
                    for ent, emb in zip(chunk, embeddings):
                        ent.embedding = emb.tolist()
                    index_q.put(chunk)

            # Flush remaining
            if pending:
                texts = [e.to_search_text() for e in pending]
                embeddings = self.encoder.encode(texts, show_progress_bar=False, batch_size=len(pending))
                for ent, emb in zip(pending, embeddings):
                    ent.embedding = emb.tolist()
                index_q.put(pending)

            index_q.put(_DONE)

        # ── Stage 3: Index to OpenSearch (bulk upsert, I/O-bound) ────
        def stage_index():
            while True:
                batch = index_q.get()
                if batch is _DONE:
                    break
                try:
                    self.store.index(batch)
                    for ent in batch:
                        self.ui_mapper.learn_from_entity(ent)
                    total_indexed[0] += len(batch)
                except Exception as e:
                    errors.append(str(e))
                    logger.error(f"Index error: {e}")

        # ── Launch all 3 stages concurrently ─────────────────────────
        t_parse = threading.Thread(target=stage_parse, name="parse")
        t_embed = threading.Thread(target=stage_embed, name="embed")
        t_index = threading.Thread(target=stage_index, name="index")

        t_parse.start()
        t_embed.start()
        t_index.start()

        t_parse.join()
        t_embed.join()
        t_index.join()

        if errors:
            logger.warning(f"Indexing completed with {len(errors)} errors")

        return total_indexed[0]

    def localize(self, error_text: str, top_k: int = 5, namespace: str = None) -> list[dict]:
        """Localize fault from stack trace or answer natural language code questions."""
        error = self._extract_error(error_text)
        is_nl_query = error.exception_type in ("NLQuery", "Unknown") and not error.frames

        # Auto-detect namespace from stack trace file paths
        if not namespace:
            namespace = self._detect_namespace(error)

        if is_nl_query:
            # Natural language query — use raw text directly as search query
            query = error_text.strip()
        else:
            # Stack trace — build structured query from extracted info
            query = f"{error.exception_type} {error.message} {' '.join(error.method_names)}"

        query_embedding = self.encoder.encode(query).tolist()

        direct_candidates = []
        if not is_nl_query:
            for frame in error.frames:
                entities = self.store.get_by_name(frame.method_name, namespace=namespace)
                for entity in entities:
                    direct_candidates.append((entity, 1.0))

        search_results = self.store.search_hybrid(query, query_embedding, top_k=50, namespace=namespace)
        all_candidates = self._merge_candidates(direct_candidates, search_results)

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
