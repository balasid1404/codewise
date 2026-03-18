"""Graph-based score propagation for fault localization.

Implements suspiciousness propagation along the call graph:
- If method B is suspicious and A calls B, A gets a propagated boost
- If method B is suspicious and B calls C, C gets a smaller boost
- Scores decay with graph distance (configurable damping factor)

Inspired by SBFL/MBFL research (Ochiai, DStar) adapted for retrieval-based FL.
"""

import logging
from collections import defaultdict
from indexer.entities import CodeEntity
from storage.opensearch_store import OpenSearchStore

logger = logging.getLogger(__name__)

# Damping factor per hop (score multiplied by this for each graph hop)
DAMPING = 0.4
# Max hops to propagate
MAX_DEPTH = 2
# Minimum score to propagate (don't propagate noise)
MIN_PROPAGATE_SCORE = 0.1


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
        """
        Given initial scored candidates, propagate scores through the call graph.

        For each candidate with score S:
          - Its callers get S * damping added (they invoked the suspicious code)
          - Its callees get S * damping * 0.5 added (they might be the root cause)
          - Propagation continues up to max_depth hops with compounding damping

        Returns re-scored and expanded candidate list (may include new entities
        discovered via graph traversal).
        """
        if not candidates:
            return candidates

        # Build score map from initial candidates
        score_map: dict[str, float] = {}
        entity_map: dict[str, CodeEntity] = {}

        for entity, score in candidates:
            score_map[entity.id] = score
            entity_map[entity.id] = entity

        # Collect all entity names we need to resolve calls for
        propagation_queue: list[tuple[str, float, int]] = []  # (entity_id, score, depth)
        for entity, score in candidates:
            if score >= MIN_PROPAGATE_SCORE:
                propagation_queue.append((entity.id, score, 0))

        # BFS propagation
        visited = set()
        for eid, base_score, depth in propagation_queue:
            if depth >= max_depth:
                continue
            if eid in visited:
                continue
            visited.add(eid)

            entity = entity_map.get(eid)
            if not entity:
                continue

            # Propagate to callers (upstream — they invoked suspicious code)
            caller_boost = base_score * damping
            callers = self._get_callers(entity.name, namespace)
            for caller in callers:
                cid = caller.id
                if cid not in entity_map:
                    entity_map[cid] = caller
                old = score_map.get(cid, 0)
                score_map[cid] = old + caller_boost
                if depth + 1 < max_depth and caller_boost >= MIN_PROPAGATE_SCORE:
                    propagation_queue.append((cid, caller_boost, depth + 1))

            # Propagate to callees (downstream — potential root cause)
            callee_boost = base_score * damping * 0.5
            callees = self._resolve_calls(entity.calls, namespace)
            for callee in callees:
                cid = callee.id
                if cid not in entity_map:
                    entity_map[cid] = callee
                old = score_map.get(cid, 0)
                score_map[cid] = old + callee_boost
                if depth + 1 < max_depth and callee_boost >= MIN_PROPAGATE_SCORE:
                    propagation_queue.append((cid, callee_boost, depth + 1))

        # Build final list
        result = [(entity_map[eid], score) for eid, score in score_map.items()]
        result.sort(key=lambda x: x[1], reverse=True)
        return result

    def _get_callers(self, name: str, namespace: str | None) -> list[CodeEntity]:
        """Find entities whose `calls` field contains this name."""
        try:
            filter_clauses = [{"term": {"calls": name}}]
            if namespace:
                filter_clauses.append({"term": {"namespace": namespace}})
            resp = self.store.client.search(
                index=self.store.INDEX_NAME,
                body={"size": 10, "query": {"bool": {"filter": filter_clauses}}}
            )
            return [e for e, _ in self.store._hits_to_entities(resp["hits"]["hits"])]
        except Exception as e:
            logger.debug(f"Graph caller lookup failed for {name}: {e}")
            return []

    def _resolve_calls(self, call_names: list[str], namespace: str | None) -> list[CodeEntity]:
        """Resolve call names to actual entities."""
        results = []
        for name in call_names[:10]:  # limit to avoid excessive queries
            try:
                entities = self.store.get_by_name(name, namespace=namespace)
                results.extend(entities[:2])  # max 2 per call name
            except Exception:
                pass
        return results
