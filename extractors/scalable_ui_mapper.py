"""Scalable UI Mapper using OpenSearch for vocabulary storage."""

import re
from typing import Optional


class ScalableUIMapper:
    """
    UI Mapper that uses OpenSearch for vocabulary lookup.
    
    Instead of in-memory vocabulary:
    - Stores word->entity mappings in OpenSearch during indexing
    - Queries OpenSearch for pattern matching
    - Scales to 30M+ entities
    """

    VOCAB_INDEX = "ui_vocabulary"

    def __init__(self, opensearch_client):
        self.client = opensearch_client
        self._ensure_index()

    def _ensure_index(self):
        """Create vocabulary index if not exists."""
        if not self.client.indices.exists(self.VOCAB_INDEX):
            self.client.indices.create(
                index=self.VOCAB_INDEX,
                body={
                    "settings": {"number_of_shards": 3},
                    "mappings": {
                        "properties": {
                            "word": {"type": "keyword"},
                            "entity_names": {"type": "keyword"},
                            "entity_count": {"type": "integer"},
                            "source": {"type": "keyword"}  # name, class, docstring
                        }
                    }
                }
            )

    def learn_from_entity(self, entity) -> None:
        """Index vocabulary from a single entity (called during indexing)."""
        words_to_index = []

        # From entity name
        for word in self._extract_words(entity.name):
            words_to_index.append((word.lower(), entity.full_name, "name"))

        # From class name
        if entity.class_name:
            for word in self._extract_words(entity.class_name):
                words_to_index.append((word.lower(), entity.full_name, "class"))

        # From docstring (limited)
        if entity.docstring:
            for word in self._extract_words(entity.docstring)[:5]:
                if len(word) > 3:
                    words_to_index.append((word.lower(), entity.full_name, "docstring"))

        # Batch upsert to OpenSearch
        for word, entity_name, source in words_to_index:
            self._upsert_word(word, entity_name, source)

    def _upsert_word(self, word: str, entity_name: str, source: str):
        """Add entity to word's mapping (upsert pattern)."""
        doc_id = f"{word}_{source}"

        try:
            # Try to update existing
            self.client.update(
                index=self.VOCAB_INDEX,
                id=doc_id,
                body={
                    "script": {
                        "source": """
                            if (!ctx._source.entity_names.contains(params.entity)) {
                                ctx._source.entity_names.add(params.entity);
                                ctx._source.entity_count += 1;
                            }
                        """,
                        "params": {"entity": entity_name}
                    },
                    "upsert": {
                        "word": word,
                        "entity_names": [entity_name],
                        "entity_count": 1,
                        "source": source
                    }
                }
            )
        except Exception:
            # Insert new
            self.client.index(
                index=self.VOCAB_INDEX,
                id=doc_id,
                body={
                    "word": word,
                    "entity_names": [entity_name],
                    "entity_count": 1,
                    "source": source
                }
            )

    def get_code_patterns(self, ui_text: str, limit: int = 50) -> list[str]:
        """Query OpenSearch for code patterns matching UI text."""
        ui_words = self._extract_words(ui_text)
        if not ui_words:
            return []

        # Search vocabulary index
        response = self.client.search(
            index=self.VOCAB_INDEX,
            body={
                "size": 100,
                "query": {
                    "bool": {
                        "should": [
                            {"terms": {"word": [w.lower() for w in ui_words]}},
                            {"wildcard": {"word": f"*{ui_words[0].lower()}*"}} if ui_words else {}
                        ]
                    }
                },
                "sort": [{"entity_count": "desc"}]
            }
        )

        # Collect unique entity names
        seen = set()
        patterns = []
        for hit in response["hits"]["hits"]:
            for entity_name in hit["_source"]["entity_names"]:
                if entity_name not in seen:
                    patterns.append(entity_name)
                    seen.add(entity_name)
                    if len(patterns) >= limit:
                        return patterns

        return patterns

    def suggest_file_patterns(self, app_section: str) -> list[str]:
        """Get file patterns from entities matching the section."""
        patterns = self.get_code_patterns(app_section, limit=20)

        # Query main index for file paths
        if not patterns:
            return [f"*{app_section.lower()}*"]

        # This would query the main code_entities index
        # For now, return generic patterns
        return [
            f"*{app_section.lower()}*",
            f"*/{app_section.lower()}/*"
        ]

    def build_search_context(self, extracted: dict) -> dict:
        """Build search context from extracted image data."""
        code_patterns = []

        # From UI elements
        for elem in extracted.get("ui_elements", []):
            code_patterns.extend(self.get_code_patterns(elem, limit=10))

        # From app section
        if extracted.get("app_section"):
            code_patterns.extend(self.get_code_patterns(extracted["app_section"], limit=10))

        # From keywords
        for kw in extracted.get("keywords", []):
            code_patterns.extend(self.get_code_patterns(kw, limit=5))

        # From error message
        if extracted.get("error_message"):
            code_patterns.extend(self.get_code_patterns(extracted["error_message"], limit=10))

        # Dedupe
        seen = set()
        unique = [p for p in code_patterns if not (p in seen or seen.add(p))]

        return {
            "code_patterns": unique[:50],
            "file_patterns": self.suggest_file_patterns(extracted.get("app_section", "")),
            "error_text": extracted.get("error_message", ""),
            "context": extracted.get("user_action", "")
        }

    def _extract_words(self, text: str) -> list[str]:
        """Extract words from text."""
        if not text:
            return []
        text = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)
        text = text.replace("_", " ")
        words = re.findall(r'[a-zA-Z]+', text)
        return [w for w in words if len(w) > 2]
