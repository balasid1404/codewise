"""Graph-based score propagation for fault localization.

Implements suspiciousness propagation along the call graph:
- If method B is suspicious and A calls B, A gets a propagated boost
- If method B is suspicious and B calls C, C gets a smaller boost
- Scores decay with graph distance (configurable damping factor)

Uses resolved_calls (entity IDs) when available for exact graph traversal.
Falls back to fuzzy name matching via msearch when resolved_calls is empty.

Inspired by SBFL/MBFL research (Ochiai, DStar) adapted for retrieval-based FL.
"""

import logging
from indexer.entities import CodeEntity
from storage.opensearch_store import OpenSearchStore

logger = logging.getLogger(__name__)

DAMPING = 0.4
MAX_DEPTH = 2
MIN_PROPAGATE_SCORE = 0.15
MAX_PROPAGATE_CANDIDATES = 10


class GraphRanker:
    """Propagate suspiciousness scores along the call graph stored in OpenSearch."""

    def __init__(self, store: OpenSearchStore):
        self.store = store

    def propagate(
        self,
        candidates: list[tuple[CodeEntity, float]],
        namespace: str | None = None,
        damping: float = DAMPING,
        max_depth: int = MAX_DEPTH,
    ) -> list[tuple[CodeEntity, float]]:
        if not candidates:
            return candidates

        score_map: dict[str, float] = {}
        entity_map: dict[str, CodeEntity] = {}

        for entity, score in candidates:
            score_map[entity.id] = score
            entity_map[entity.id] = entity

        seeds = [
            (entity, score)
            for entity, score in candidates[:MAX_PROPAGATE_CANDIDATES]
            if score >= MIN_PROPAGATE_SCORE
        ]

        # Depth 0 → 1
        if seeds:
            self._propagate_batch(seeds, score_map, entity_map, namespace, damping)

        # Depth 1 → 2
        if max_depth >= 2:
            original_ids = {e.id for e, _ in candidates}
            new_seeds = [
                (entity_map[eid], score)
                for eid, score in score_map.items()
                if eid not in original_ids and score >= MIN_PROPAGATE_SCORE
            ][:MAX_PROPAGATE_CANDIDATES]
            if new_seeds:
                self._propagate_batch(new_seeds, score_map, entity_map, namespace, damping)

        result = [(entity_map[eid], score) for eid, score in score_map.items()]
        result.sort(key=lambda x: x[1], reverse=True)
        return result

    def _propagate_batch(
        self,
        seeds: list[tuple[CodeEntity, float]],
        score_map: dict[str, float],
        entity_map: dict[str, CodeEntity],
        namespace: str | None,
        damping: float,
    ) -> None:
        """Batch-propagate scores using resolved_calls (exact IDs) + caller lookup."""

        # Separate: entities with resolved_calls (exact) vs without (fuzzy fallback)
        exact_callee_ids: list[tuple[str, float]] = []  # (entity_id, boost)
        fuzzy_callee_names: list[tuple[str, float]] = []  # (call_name, boost)
        caller_lookups: list[tuple[str, float]] = []  # (entity_name, boost)

        for entity, score in seeds:
            caller_lookups.append((entity.name, score * damping))

            callee_boost = score * damping * 0.5
            if entity.resolved_calls:
                # Exact: use resolved entity IDs
                for eid in entity.resolved_calls[:8]:
                    exact_callee_ids.append((eid, callee_boost))
            else:
                # Fallback: fuzzy name matching
                for call_name in entity.calls[:8]:
                    fuzzy_callee_names.append((call_name, callee_boost))

        # --- Fetch exact callees by ID (single mget) ---
        if exact_callee_ids:
            unique_ids = list({eid for eid, _ in exact_callee_ids})
            boost_by_id = {}
            for eid, boost in exact_callee_ids:
                boost_by_id[eid] = boost_by_id.get(eid, 0) + boost

            try:
                resp = self.store.client.mget(
                    index=self.store.INDEX_NAME,
                    body={"ids": unique_ids[:50]}
                )
                for doc in resp.get("docs", []):
                    if not doc.get("found"):
                        continue
                    entities = self.store._hits_to_entities([{"_source": doc["_source"], "_score": 1.0}])
                    if entities:
                        entity, _ = entities[0]
                        if entity.id not in entity_map:
                            entity_map[entity.id] = entity
                        old = score_map.get(entity.id, 0)
                        score_map[entity.id] = old + boost_by_id.get(entity.id, 0)
            except Exception as e:
                logger.debug(f"Graph mget failed: {e}")

        # --- Build msearch for callers + fuzzy callees ---
        msearch_body = []
        query_meta = []

        for name, boost in caller_lookups:
            filter_clauses = [{"term": {"calls": name}}]
            if namespace:
                filter_clauses.append({"term": {"namespace": namespace}})
            msearch_body.append({"index": self.store.INDEX_NAME})
            msearch_body.append({"size": 5, "query": {"bool": {"filter": filter_clauses}}})
            query_meta.append(boost)

        for name, boost in fuzzy_callee_names:
            query = {"bool": {"should": [
                {"term": {"name": name}},
                {"wildcard": {"full_name": f"*{name}"}}
            ]}}
            if namespace:
                query = {"bool": {"must": query, "filter": {"term": {"namespace": namespace}}}}
            msearch_body.append({"index": self.store.INDEX_NAME})
            msearch_body.append({"size": 2, "query": query})
            query_meta.append(boost)

        if not msearch_body:
            return

        try:
            resp = self.store.client.msearch(body=msearch_body)
        except Exception as e:
            logger.debug(f"Graph msearch failed: {e}")
            return

        for i, response in enumerate(resp.get("responses", [])):
            if response.get("error"):
                continue
            hits = response.get("hits", {}).get("hits", [])
            if not hits:
                continue

            boost = query_meta[i]
            entities = self.store._hits_to_entities(hits)

            for entity, _ in entities:
                eid = entity.id
                if eid not in entity_map:
                    entity_map[eid] = entity
                old = score_map.get(eid, 0)
                score_map[eid] = old + boost
