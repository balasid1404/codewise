"""OpenSearch storage backend for production scale."""

from opensearchpy import OpenSearch, helpers
from indexer.entities import CodeEntity, EntityType
from .base import VectorStore


class OpenSearchStore(VectorStore):
    INDEX_NAME = "code_entities"

    def __init__(self, host: str = "localhost", port: int = 9200, use_ssl: bool = False):
        self.client = OpenSearch(
            hosts=[{"host": host, "port": port}],
            use_ssl=use_ssl,
            verify_certs=False,
            ssl_show_warn=False
        )
        self._ensure_index()

    def _ensure_index(self):
        if self.client.indices.exists(index=self.INDEX_NAME):
            # Check if namespace field exists in mapping; if not, recreate index
            mapping = self.client.indices.get_mapping(index=self.INDEX_NAME)
            props = mapping.get(self.INDEX_NAME, {}).get("mappings", {}).get("properties", {})
            if "namespace" not in props:
                self.client.indices.delete(index=self.INDEX_NAME)
            else:
                return
        self.client.indices.create(
                index=self.INDEX_NAME,
                body={
                    "settings": {
                        "index": {"knn": True, "number_of_shards": 5, "number_of_replicas": 1}
                    },
                    "mappings": {
                        "properties": {
                            "id": {"type": "keyword"},
                            "namespace": {"type": "keyword"},
                            "name": {"type": "keyword"},
                            "full_name": {"type": "keyword"},
                            "entity_type": {"type": "keyword"},
                            "file_path": {"type": "keyword"},
                            "package": {"type": "keyword"},
                            "class_name": {"type": "keyword"},
                            "start_line": {"type": "integer"},
                            "end_line": {"type": "integer"},
                            "signature": {"type": "text", "analyzer": "standard"},
                            "body": {"type": "text", "analyzer": "standard"},
                            "docstring": {"type": "text", "analyzer": "standard"},
                            "search_text": {"type": "text", "analyzer": "standard"},
                            "embedding": {
                                "type": "knn_vector",
                                "dimension": 768,
                                "method": {"name": "hnsw", "space_type": "cosinesimil", "engine": "nmslib"}
                            },
                            "calls": {"type": "keyword"},
                            "resolved_calls": {"type": "keyword"},
                            "imports": {"type": "keyword"},
                            "annotations": {"type": "keyword"},
                            "base_classes": {"type": "keyword"},
                            "file_imports": {"type": "keyword"},
                            "references": {"type": "keyword"},
                        }
                    }
                }
            )

    def index(self, entities: list[CodeEntity], batch_size: int = 500) -> int:
        actions = []
        for entity in entities:
            doc = {
                "_index": self.INDEX_NAME,
                "_id": entity.id,
                "_source": {
                    "id": entity.id,
                    "namespace": entity.namespace or "default",
                    "name": entity.name,
                    "full_name": entity.full_name,
                    "entity_type": entity.entity_type.value,
                    "file_path": entity.file_path,
                    "package": entity.package,
                    "class_name": entity.class_name,
                    "start_line": entity.start_line,
                    "end_line": entity.end_line,
                    "signature": entity.signature,
                    "body": entity.body[:5000],  # Truncate large bodies
                    "docstring": entity.docstring,
                    "search_text": entity.to_search_text(),
                    "embedding": entity.embedding,
                    "calls": entity.calls,
                    "resolved_calls": entity.resolved_calls,
                    "imports": entity.imports,
                    "annotations": entity.annotations,
                    "base_classes": entity.base_classes,
                    "file_imports": entity.file_imports,
                    "references": entity.references,
                }
            }
            actions.append(doc)

            if len(actions) >= batch_size:
                helpers.bulk(self.client, actions)
                actions = []

        if actions:
            helpers.bulk(self.client, actions)

        self.client.indices.refresh(index=self.INDEX_NAME)
        return len(entities)

    def search_bm25(self, query: str, top_k: int = 100, namespace: str = None) -> list[tuple[CodeEntity, float]]:
        body = {
            "size": top_k,
            "query": {
                "multi_match": {
                    "query": query,
                    "fields": ["search_text^3", "signature^2", "name^2", "body", "docstring"]
                }
            }
        }
        if namespace:
            body["query"] = {
                "bool": {
                    "must": body["query"],
                    "filter": {"term": {"namespace": namespace}}
                }
            }
        response = self.client.search(index=self.INDEX_NAME, body=body)
        return self._hits_to_entities(response["hits"]["hits"])

    def search_vector(self, embedding: list[float], top_k: int = 20, namespace: str = None) -> list[tuple[CodeEntity, float]]:
        body = {
            "size": top_k,
            "query": {"knn": {"embedding": {"vector": embedding, "k": top_k}}}
        }
        if namespace:
            body["query"] = {
                "bool": {
                    "must": body["query"],
                    "filter": {"term": {"namespace": namespace}}
                }
            }
        response = self.client.search(index=self.INDEX_NAME, body=body)
        return self._hits_to_entities(response["hits"]["hits"])

    def search_hybrid(self, query: str, embedding: list[float], top_k: int = 20, bm25_weight: float = 0.4, namespace: str = None) -> list[tuple[CodeEntity, float]]:
        # BM25 first pass
        bm25_results = self.search_bm25(query, top_k=100, namespace=namespace)
        if not bm25_results:
            return []

        # Get IDs for vector search filter
        ids = [e.id for e, _ in bm25_results]

        # Vector search on BM25 candidates
        filter_clauses = [{"terms": {"id": ids}}]
        if namespace:
            filter_clauses.append({"term": {"namespace": namespace}})

        response = self.client.search(
            index=self.INDEX_NAME,
            body={
                "size": top_k,
                "query": {
                    "bool": {
                        "must": {"knn": {"embedding": {"vector": embedding, "k": top_k * 2}}},
                        "filter": filter_clauses
                    }
                }
            }
        )
        return self._hits_to_entities(response["hits"]["hits"])

    def get_by_file(self, file_path: str) -> list[CodeEntity]:
        response = self.client.search(
            index=self.INDEX_NAME,
            body={"size": 100, "query": {"term": {"file_path": file_path}}}
        )
        return [e for e, _ in self._hits_to_entities(response["hits"]["hits"])]

    def get_by_name(self, name: str, namespace: str = None) -> list[CodeEntity]:
        query = {
            "bool": {
                "should": [
                    {"term": {"name": name}},
                    {"wildcard": {"full_name": f"*{name}"}},
                    {"term": {"class_name": name}},
                ]
            }
        }
        if namespace:
            query = {"bool": {"must": query, "filter": {"term": {"namespace": namespace}}}}
        response = self.client.search(
            index=self.INDEX_NAME,
            body={"size": 100, "query": query}
        )
        return [e for e, _ in self._hits_to_entities(response["hits"]["hits"])]

    def search_references(self, identifier: str, top_k: int = 200, namespace: str = None) -> list[tuple[CodeEntity, float]]:
        """Gap 1: Find all entities that reference a given constant/field name.

        Searches the 'references' keyword field (exact match) and falls back
        to BM25 body/search_text search for the identifier string.
        Returns ALL matching entities (not just top-k by relevance).
        """
        # Exact match on the references keyword field
        must_clauses = [
            {"bool": {"should": [
                {"term": {"references": identifier}},
                {"term": {"name": identifier}},
                {"match_phrase": {"body": identifier}},
                {"match_phrase": {"search_text": identifier}},
            ]}}
        ]
        if namespace:
            query = {"bool": {"must": must_clauses, "filter": {"term": {"namespace": namespace}}}}
        else:
            query = {"bool": {"must": must_clauses}}

        response = self.client.search(
            index=self.INDEX_NAME,
            body={"size": top_k, "query": query}
        )
        return self._hits_to_entities(response["hits"]["hits"])

    def search_references_cross_namespace(self, identifier: str, top_k: int = 200) -> list[tuple[CodeEntity, float]]:
        """Gap 2: Search for references across ALL namespaces.

        For rename/refactor queries, we need completeness across repos.
        """
        return self.search_references(identifier, top_k=top_k, namespace=None)

    def list_namespaces(self) -> list[dict]:
        """List all indexed namespaces with entity counts."""
        response = self.client.search(
            index=self.INDEX_NAME,
            body={
                "size": 0,
                "aggs": {
                    "namespaces": {
                        "terms": {"field": "namespace", "size": 10000}
                    }
                }
            }
        )
        return [
            {"namespace": b["key"], "count": b["doc_count"]}
            for b in response["aggregations"]["namespaces"]["buckets"]
        ]

    def search_namespaces(self, query: str, limit: int = 10) -> list[dict]:
        """Search namespaces by prefix for typeahead autocomplete."""
        response = self.client.search(
            index=self.INDEX_NAME,
            body={
                "size": 0,
                "aggs": {
                    "namespaces": {
                        "terms": {"field": "namespace", "size": 10000}
                    }
                }
            }
        )
        q = query.lower()
        matches = [
            {"namespace": b["key"], "count": b["doc_count"]}
            for b in response["aggregations"]["namespaces"]["buckets"]
            if q in b["key"].lower()
        ]
        return matches[:limit]

    def get_dependencies(self, entity_id: str, namespace: str = None) -> dict:
        """Get dependency tree for an entity: what it calls, what calls it, its imports, and siblings in same file."""
        # Get the entity itself
        try:
            doc = self.client.get(index=self.INDEX_NAME, id=entity_id)
        except Exception:
            return {"error": "Entity not found"}

        src = doc["_source"]
        entity_name = src.get("full_name") or src.get("name")
        entity_calls = src.get("calls", [])
        entity_imports = src.get("imports", [])
        entity_file = src.get("file_path")
        ns = namespace or src.get("namespace")

        # 1. What this entity calls (callees) — use resolved_calls (exact IDs) first, fallback to name
        callees = []
        resolved = src.get("resolved_calls", [])
        if resolved:
            # Exact: fetch by IDs
            try:
                mget_resp = self.client.mget(index=self.INDEX_NAME, body={"ids": resolved[:20]})
                for doc in mget_resp.get("docs", []):
                    if doc.get("found"):
                        callees.append(self._hit_to_summary({"_source": doc["_source"]}))
            except Exception:
                pass
        elif entity_calls:
            for call_name in entity_calls[:20]:
                results = self.get_by_name(call_name, namespace=ns)
                for e in results[:2]:
                    callees.append(self._entity_to_summary(e))

        # 2. What calls this entity (callers) — search for entities whose calls field contains this name
        callers = []
        name = src.get("name")
        if name:
            filter_clauses = [{"term": {"calls": name}}]
            if ns:
                filter_clauses.append({"term": {"namespace": ns}})
            try:
                resp = self.client.search(
                    index=self.INDEX_NAME,
                    body={"size": 20, "query": {"bool": {"filter": filter_clauses}}}
                )
                for hit in resp["hits"]["hits"]:
                    callers.append(self._hit_to_summary(hit))
            except Exception:
                pass

        # 3. Siblings — other entities in the same file
        siblings = []
        if entity_file:
            file_entities = self.get_by_file(entity_file)
            for e in file_entities:
                if e.id != entity_id:
                    siblings.append(self._entity_to_summary(e))

        return {
            "entity": {
                "id": src["id"],
                "name": entity_name,
                "file_path": entity_file,
                "signature": src.get("signature"),
                "annotations": src.get("annotations", []),
            },
            "imports": entity_imports,
            "calls": callees,
            "called_by": callers,
            "same_file": siblings[:10],
        }

    def _entity_to_summary(self, entity: CodeEntity) -> dict:
        return {
            "id": entity.id,
            "name": entity.full_name,
            "file_path": entity.file_path,
            "start_line": entity.start_line,
            "signature": entity.signature,
            "entity_type": entity.entity_type.value,
        }

    def _hit_to_summary(self, hit: dict) -> dict:
        src = hit["_source"]
        return {
            "id": src["id"],
            "name": src.get("full_name") or src.get("name"),
            "file_path": src.get("file_path"),
            "start_line": src.get("start_line"),
            "signature": src.get("signature"),
            "entity_type": src.get("entity_type"),
        }

    def _hits_to_entities(self, hits: list) -> list[tuple[CodeEntity, float]]:
        results = []
        for hit in hits:
            src = hit["_source"]
            entity = CodeEntity(
                id=src["id"],
                name=src["name"],
                entity_type=EntityType(src["entity_type"]),
                file_path=src["file_path"],
                start_line=src["start_line"],
                end_line=src["end_line"],
                signature=src["signature"],
                body=src["body"],
                class_name=src.get("class_name"),
                package=src.get("package"),
                docstring=src.get("docstring"),
                embedding=src.get("embedding"),
                calls=src.get("calls", []),
                namespace=src.get("namespace"),
                imports=src.get("imports", []),
                annotations=src.get("annotations", []),
                resolved_calls=src.get("resolved_calls", []),
                base_classes=src.get("base_classes", []),
                file_imports=src.get("file_imports", []),
                references=src.get("references", []),
            )
            results.append((entity, hit["_score"]))
        return results
