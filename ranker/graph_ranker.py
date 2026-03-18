"""Graph-based score propagation for fault localization.

Implements suspiciousness propagation along the call graph:
- If method B is suspicious and A calls B, A gets a propagated boost
- If method B is suspicious and B calls C, C gets a smaller boost
- Scores decay with graph distance (configurable damping factor)

Uses OpenSearch msearch (multi-search) to batch all graph lookups into
1-2 network round-trips instead of N sequential queries.

Inspired by SBFL/MBFL research (Ochiai, DStar) adapted for retrieval-based FL.
"""

import logging
from indexer.entities import CodeEntity
from storage.opensearch_store import OpenSearchStore

logger = logging.getLogger(__name__)

DAMPING = 0.4
MAX_DEPTH = 2
MIN_PROPAGATE_SCORE = 0.15
# Only propagate from top N candidates (avoids noise and excess queries)
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

        # Only propagate from top candidates (sorted by score desc already)
        seeds = [
            (entity, score)
            for entity, score in candidates[:MAX_PROPAGATE_CANDIDATES]
            if score >= MIN_PROPAGATE_SCORE
        ]

        # Depth 0 → 1 propagation
        if seeds:
            self._propagate_batch(seeds, score_map, entity_map, namespace, damping)

        # Depth 1 → 2 propagation (from newly discovered entities)
        if max_depth >= 2:
            new_seeds = [
                (entity_map[eid], score)
                for eid, score in score_map.items()
                if eid not in {e.id for e, _ in candidates}
                and score >= MIN_PROPAGATE_SCORE
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
        """Batch-propagate scores from seed entities using msearch."""

        # Collect unique names for caller lookups and callee lookups
        caller_lookups: list[tuple[str, float]] = []  # (entity_name, boost)
        callee_lookups: list[tuple[str, float]] = []   # (call_name, boost)

        for entity, score in seeds:
            caller_lookups.append((entity.name, score * damping))
            for call_name in entity.calls[:8]:
                callee_lookups.append((call_name, score * damping * 0.5))

        # Build msearch body — all caller + callee queries in one request
        msearch_body = []
        query_meta = []  # track what each query is for: ("caller", boost) or ("callee", boost)

        for name, boost in caller_lookups:
            filter_clauses = [{"term": {"calls": name}}]
            if namespace:
                filter_clauses.append({"term": {"namespace": namespace}})
            msearch_body.append({"index": self.store.INDEX_NAME})
            msearch_body.append({"size": 5, "query": {"bool": {"filter": filter_clauses}}})
            query_meta.append(("caller", boost))

        for name, boost in callee_lookups:
            query = {
                "bool": {
                    "should": [
                        {"term": {"name": name}},
                        {"wildcard": {"full_name": f"*{name}"}}
                    ]
                }
            }
            if namespace:
                query = {"bool": {"must": query, "filter": {"term": {"namespace": namespace}}}}
            msearch_body.append({"index": self.store.INDEX_NAME})
            msearch_body.append({"size": 2, "query": query})
            query_meta.append(("callee", boost))

        if not msearch_body:
            return

        # Execute all queries in one round-trip
        try:
            resp = self.store.client.msearch(body=msearch_body)
        except Exception as e:
            logger.debug(f"Graph msearch failed: {e}")
            return

        # Process results
        for i, response in enumerate(resp.get("responses", [])):
            if response.get("error"):
                continue
            hits = response.get("hits", {}).get("hits", [])
            if not hits:
                continue

            query_type, boost = query_meta[i]
            entities = self.store._hits_to_entities(hits)

            for entity, _ in entities:
                eid = entity.id
                if eid not in entity_map:
                    entity_map[eid] = entity
                old = score_map.get(eid, 0)
                score_map[eid] = old + boost
