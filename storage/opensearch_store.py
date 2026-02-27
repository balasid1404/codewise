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
        if not self.client.indices.exists(self.INDEX_NAME):
            self.client.indices.create(
                index=self.INDEX_NAME,
                body={
                    "settings": {
                        "index": {"knn": True, "number_of_shards": 5, "number_of_replicas": 1}
                    },
                    "mappings": {
                        "properties": {
                            "id": {"type": "keyword"},
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
                }
            }
            actions.append(doc)

            if len(actions) >= batch_size:
                helpers.bulk(self.client, actions)
                actions = []

        if actions:
            helpers.bulk(self.client, actions)

        self.client.indices.refresh(self.INDEX_NAME)
        return len(entities)

    def search_bm25(self, query: str, top_k: int = 100) -> list[tuple[CodeEntity, float]]:
        response = self.client.search(
            index=self.INDEX_NAME,
            body={
                "size": top_k,
                "query": {
                    "multi_match": {
                        "query": query,
                        "fields": ["search_text^3", "signature^2", "name^2", "body", "docstring"]
                    }
                }
            }
        )
        return self._hits_to_entities(response["hits"]["hits"])

    def search_vector(self, embedding: list[float], top_k: int = 20) -> list[tuple[CodeEntity, float]]:
        response = self.client.search(
            index=self.INDEX_NAME,
            body={
                "size": top_k,
                "query": {"knn": {"embedding": {"vector": embedding, "k": top_k}}}
            }
        )
        return self._hits_to_entities(response["hits"]["hits"])

    def search_hybrid(self, query: str, embedding: list[float], top_k: int = 20, bm25_weight: float = 0.4) -> list[tuple[CodeEntity, float]]:
        # BM25 first pass
        bm25_results = self.search_bm25(query, top_k=100)
        if not bm25_results:
            return []

        # Get IDs for vector search filter
        ids = [e.id for e, _ in bm25_results]

        # Vector search on BM25 candidates
        response = self.client.search(
            index=self.INDEX_NAME,
            body={
                "size": top_k,
                "query": {
                    "bool": {
                        "must": {"knn": {"embedding": {"vector": embedding, "k": top_k * 2}}},
                        "filter": {"terms": {"id": ids}}
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

    def get_by_name(self, name: str) -> list[CodeEntity]:
        response = self.client.search(
            index=self.INDEX_NAME,
            body={
                "size": 100,
                "query": {
                    "bool": {
                        "should": [
                            {"term": {"name": name}},
                            {"wildcard": {"full_name": f"*{name}"}}
                        ]
                    }
                }
            }
        )
        return [e for e, _ in self._hits_to_entities(response["hits"]["hits"])]

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
                calls=src.get("calls", [])
            )
            results.append((entity, hit["_score"]))
        return results
