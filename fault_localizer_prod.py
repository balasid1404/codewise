"""Production fault localizer with OpenSearch backend."""

import queue
import re
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

# Patterns that indicate a rename/reference query (A → B, A to B)
_RENAME_PATTERNS = [
    re.compile(r'\brename\b.*\bto\b', re.IGNORECASE),
    re.compile(r'\bchange\b.*\bto\b', re.IGNORECASE),
    re.compile(r'\breplace\b.*\bwith\b', re.IGNORECASE),
    re.compile(r'\bupdate\b.*\bto\b', re.IGNORECASE),
    re.compile(r'\bmigrate\b.*\bto\b', re.IGNORECASE),
    re.compile(r'\b(\w+)\s*(?:→|->|=>)\s*(\w+)', re.IGNORECASE),
    # "find all usages of X", "where is X used", "files that reference X"
    re.compile(r'\b(?:find|search|locate)\b.*\b(?:usage|reference|occurrence)', re.IGNORECASE),
    re.compile(r'\bwhere\b.*\b(?:used|referenced|called|defined)', re.IGNORECASE),
    re.compile(r'\b(?:files?|classes?|methods?)\b.*\b(?:that|which)\b.*\b(?:use|reference|contain|import)', re.IGNORECASE),
    re.compile(r'\b(?:all|every)\b.*\b(?:usage|reference|occurrence|place)', re.IGNORECASE),
]

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
        use_llm: bool = True,  # deprecated, LLM always enabled
        encoder_model: str = "microsoft/codebert-base",
        use_cross_encoder: bool = True,
    ):
        self.store = OpenSearchStore(host=opensearch_host, port=opensearch_port)
        self.encoder = SentenceTransformer(encoder_model)
        self.python_extractor = PythonStackExtractor()
        self.java_extractor = JavaStackExtractor()
        self.image_extractor = ImageExtractor()
        self.ui_mapper = ScalableUIMapper(self.store.client)
        self.ranker = LLMRanker()
        self.graph_ranker = GraphRanker(self.store)
        self.cross_encoder = CrossEncoderRanker() if use_cross_encoder else None

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
          1b. Detect rename/refactor intent → exhaustive reference mode
          2. Weighted stack frame scoring (top frame + first app frame boosted)
          2b. Identifier extraction from NL queries (exact-match boost)
          3. Hybrid retrieval (BM25 + vector)
          3b. Reference search across all namespaces (for rename queries)
          4. Graph-based score propagation (callers/callees of suspicious code)
          5. Cross-encoder reranking (precise query-document scoring)
          6. LLM reranking (final intelligence pass)
        """
        error = self._extract_error(error_text)
        is_nl_query = error.exception_type in ("NLQuery", "Unknown") and not error.frames

        # Gap 4: Detect rename/refactor intent
        is_rename_query = is_nl_query and self._detect_rename_intent(error_text)

        # Auto-detect namespace from stack trace file paths (only for stack traces, not NL queries)
        if not namespace and not is_nl_query:
            namespace = self._detect_namespace(error)

        if is_nl_query:
            query = error_text.strip()
        else:
            query = self._build_weighted_query(error)

        query_embedding = self.encoder.encode(query).tolist()

        # Direct candidates from stack frames (with position-based weighting)
        direct_candidates = []
        if not is_nl_query:
            direct_candidates = self._weighted_frame_lookup(error, namespace)

        # For NL queries, extract identifier-like tokens and do exact-match lookups
        if is_nl_query:
            identifiers = self._extract_identifiers(error_text)

            if is_rename_query and identifiers:
                # Gap 1 + 2 + 4: Exhaustive reference search for rename queries
                return self._localize_rename(
                    error_text, error, identifiers, query, query_embedding,
                    namespace, top_k
                )

            for ident in identifiers:
                entities = self.store.get_by_name(ident, namespace=namespace)
                for entity in entities:
                    direct_candidates.append((entity, 1.0))

                # Fast path: if identifiers produced enough exact matches, skip expensive
                # cross-encoder + LLM pipeline.
                if len(direct_candidates) >= top_k:
                    merged = self._merge_candidates(direct_candidates, [])
                    bm25_results = self.store.search_bm25(query, top_k=50, namespace=namespace)
                    merged = self._merge_candidates(merged, bm25_results)
                    merged = self.graph_ranker.propagate(merged, namespace=namespace)
                    return self.ranker.rank_and_explain(error, merged, top_k)

        search_results = self.store.search_hybrid(query, query_embedding, top_k=50, namespace=namespace)
        all_candidates = self._merge_candidates(direct_candidates, search_results)

        # Graph-based score propagation
        all_candidates = self.graph_ranker.propagate(all_candidates, namespace=namespace)

        # Cross-encoder reranking (narrows to top 15 with precise scores)
        if self.cross_encoder and self.cross_encoder.available:
            all_candidates = self.cross_encoder.rerank(query, all_candidates, top_k=15)

        return self.ranker.rank_and_explain(error, all_candidates, top_k)

    # ── Gap 4: Rename intent detection ───────────────────────────

    def _detect_rename_intent(self, text: str) -> bool:
        """Detect if the query is a rename/refactor/reference-finding task.

        Triggers exhaustive mode for:
        - Rename queries: "rename X to Y", "change X to Y"
        - Reference queries: "find all usages of X", "where is X used"
        - Any query with identifiers + action keywords implying completeness
        """
        text_lower = text.lower()
        words = set(re.findall(r'[a-z]+', text_lower))

        identifiers = self._extract_identifiers(text)
        if not identifiers:
            return False

        # Check for rename/change keywords
        if words & _RENAME_KEYWORDS:
            return True

        # Check for reference/usage keywords
        if words & _REFERENCE_KEYWORDS:
            return True

        # Check for structural patterns (X to Y, find usages of X, etc.)
        for pattern in _RENAME_PATTERNS:
            if pattern.search(text):
                return True

        return False

    # ── Gaps 1+2+4: Exhaustive rename/reference localization ────

    def _localize_rename(
        self,
        error_text: str,
        error: 'ExtractedError',
        identifiers: list[str],
        query: str,
        query_embedding: list[float],
        namespace: str | None,
        top_k: int,
    ) -> list[dict]:
        """Exhaustive reference search for rename/refactor queries.

        Strategy:
        1. Find definitions of the identifier (exact name match)
        2. Search references (cross-namespace only if no namespace specified)
        3. BM25 body search for the identifier string
        4. Search file_imports to find files importing the defining file
        5. Graph propagation to find transitive dependents
        6. Dedupe by file, return ALL affected files (not just top-k)
        """
        # Respect user's namespace choice; only go cross-namespace when unset
        search_ns = namespace  # None means cross-namespace

        all_candidates: list[tuple[CodeEntity, float]] = []

        for ident in identifiers:
            # 1. Definition lookup (exact name match)
            definitions = self.store.get_by_name(ident, namespace=search_ns)
            for entity in definitions:
                all_candidates.append((entity, 1.0))

            # 2. Reference search
            if search_ns is None:
                ref_results = self.store.search_references_cross_namespace(ident, top_k=200)
            else:
                ref_results = self.store.search_bm25(f'"{ident}"', top_k=200, namespace=search_ns)
            for entity, score in ref_results:
                all_candidates.append((entity, max(0.9, score)))

            # 3. BM25 body search for the identifier
            bm25_results = self.store.search_bm25(ident, top_k=100, namespace=search_ns)
            for entity, score in bm25_results:
                body = entity.body or ""
                if ident in body:
                    all_candidates.append((entity, 0.95))
                else:
                    all_candidates.append((entity, score * 0.5))

        # 4. Find files that import the defining files
        defining_files = set()
        for entity, score in all_candidates:
            if score >= 0.95:
                defining_files.add(entity.file_path)

        if defining_files:
            for def_file in defining_files:
                # Search for entities whose file_imports include this file
                try:
                    resp = self.store.client.search(
                        index=self.store.INDEX_NAME,
                        body={
                            "size": 100,
                            "query": {"term": {"file_imports": def_file}}
                        }
                    )
                    importers = self.store._hits_to_entities(resp["hits"]["hits"])
                    for entity, _ in importers:
                        # Check if any identifier appears in the body
                        body = entity.body or ""
                        for ident in identifiers:
                            if ident in body:
                                all_candidates.append((entity, 0.85))
                                break
                except Exception as e:
                    logger.debug(f"Import search failed: {e}")

        # 5. Hybrid search with full query text to catch broader references
        hybrid_results = self.store.search_hybrid(query, query_embedding, top_k=100, namespace=search_ns)
        for entity, score in hybrid_results:
            # Check if any identifier appears in entity body or search text
            text = (entity.body or "") + " " + (entity.name or "")
            for ident in identifiers:
                if ident in text:
                    all_candidates.append((entity, max(0.8, score)))
                    break
            else:
                all_candidates.append((entity, score * 0.4))

        # 6. Merge and dedupe
        merged = self._merge_candidates(all_candidates, [])

        # 7. Graph propagation (lighter — just 1 hop for rename queries)
        merged = self.graph_ranker.propagate(merged, namespace=search_ns)

        # For rename queries, return more results than usual top_k
        # since completeness matters more than precision
        effective_top_k = max(top_k, len([e for e, s in merged if s >= 0.5]))

        return self.ranker.rank_and_explain(error, merged, effective_top_k)

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
        pseudo_error = ExtractedError(
            exception_type="UI Bug",
            message=f"{extracted.get('app_section', 'unknown')}: {extracted.get('error_message', 'visual bug')}",
            frames=[],
            raw_text=f"User action: {extracted.get('user_action', 'unknown')}\nUI elements: {extracted.get('ui_elements', [])}\nError: {extracted.get('error_message', 'none')}"
        )
        return self.ranker.rank_and_explain(pseudo_error, all_candidates, top_k)

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

        # Auto-detect namespace from text if still not set — only for stack traces, not NL queries
        if not namespace and error_text:
            error = self._extract_error(error_text)
            has_frames = bool(error.frames)
            if has_frames:
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
            else:
                # Extract identifiers from NL text for exact-match lookups
                identifiers = self._extract_identifiers(error_text)

                # Gap 4: Detect rename intent in unified path too
                is_rename = self._detect_rename_intent(error_text)
                if is_rename and identifiers and not image_path:
                    results = self._localize_rename(
                        error_text, error, identifiers, text_query, text_embedding,
                        namespace, top_k
                    )
                    return {
                        "results": results,
                        "image_extracted": None,
                        "namespace_used": namespace,
                        "namespace_source": "rename-cross-namespace" if not namespace else "user",
                    }

                for ident in identifiers:
                    for entity in self.store.get_by_name(ident, namespace=namespace):
                        all_candidates.append((entity, 1.0))

                # Fast path: if identifiers produced enough exact matches and no image,
                # skip expensive cross-encoder. Return exact + BM25 results with LLM reasons.
                if len(all_candidates) >= top_k and not image_path:
                    bm25_results = self.store.search_bm25(text_query, top_k=50, namespace=namespace)
                    merged = self._merge_candidates(all_candidates, bm25_results)
                    merged = self.graph_ranker.propagate(merged, namespace=namespace)
                    pseudo_error = ExtractedError(
                        exception_type="NLQuery",
                        message=error_text[:300],
                        frames=[],
                        raw_text=error_text
                    )
                    results = self.ranker.rank_and_explain(pseudo_error, merged, top_k)
                    return {
                        "results": results,
                        "image_extracted": None,
                        "namespace_used": namespace,
                        "namespace_source": "user" if namespace else "all",
                    }

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
        if merged:
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
            results = []

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
            # and static initializer blocks (they contain map population code)
            body_len = len(ent.body) if ent.body else 0
            is_static_init = ent.name.startswith("<static_init")
            if body_len < 30 and ent.entity_type.value not in ("field", "enum") and not is_static_init:
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

    # ── Identifier extraction for NL queries ─────────────────────

    def _extract_identifiers(self, text: str) -> list[str]:
        """Extract code identifier-like tokens from natural language text.

        Finds patterns that look like code identifiers:
        - UPPER_SNAKE_CASE constants (e.g. HAWKFIRE_ALL_DEVICES_ANNUAL)
        - CamelCase class/method names (e.g. PlanIdFake, getSubscriptionModel)
        - dotted.qualified.names (e.g. com.amazon.digital.music)
        - quoted identifiers (e.g. "HF_AD_AND")

        Returns deduplicated list, longest identifiers first (more specific = better match).
        """
        import re
        identifiers = set()

        # UPPER_SNAKE_CASE: 2+ uppercase segments joined by underscores
        for m in re.finditer(r'\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b', text):
            identifiers.add(m.group(1))

        # CamelCase: starts with uppercase, has at least one lowercase→uppercase transition
        for m in re.finditer(r'\b([A-Z][a-z]+(?:[A-Z][a-z0-9]*)+)\b', text):
            identifiers.add(m.group(1))

        # camelCase methods: starts lowercase, has uppercase transition
        for m in re.finditer(r'\b([a-z][a-z0-9]*(?:[A-Z][a-z0-9]*)+)\b', text):
            ident = m.group(1)
            # Skip common English words that look camelCase-ish
            if len(ident) > 6:
                identifiers.add(ident)

        # Quoted identifiers: "HF_AD_AND", 'PLAN_ID'
        for m in re.finditer(r'["\']([A-Za-z_][A-Za-z0-9_]+)["\']', text):
            identifiers.add(m.group(1))

        # Dotted qualified names: com.amazon.digital.music.subs
        for m in re.finditer(r'\b([a-z][a-z0-9]*(?:\.[a-z][a-z0-9]*){2,})\b', text):
            identifiers.add(m.group(1))

        # Sort longest first — more specific identifiers are better matches
        return sorted(identifiers, key=len, reverse=True)[:20]

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
