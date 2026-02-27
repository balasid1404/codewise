"""UI Mapper that learns patterns from the indexed codebase."""

import re
from collections import defaultdict
from typing import Optional
from indexer.entities import CodeEntity


class LearnedUIMapper:
    """
    Maps UI elements to code patterns by learning from the indexed codebase.
    
    Instead of hardcoded mappings, builds a vocabulary from actual code:
    - Extracts words from method/class names
    - Groups by semantic similarity
    - Matches UI text against learned vocabulary
    """

    def __init__(self):
        self.vocabulary: dict[str, list[str]] = defaultdict(list)  # word -> [full_names]
        self.name_to_entity: dict[str, CodeEntity] = {}
        self.word_frequency: dict[str, int] = defaultdict(int)

    def learn_from_entities(self, entities: list[CodeEntity]) -> None:
        """Build vocabulary from indexed code entities."""
        for entity in entities:
            self.name_to_entity[entity.full_name] = entity

            # Extract words from entity name
            words = self._extract_words(entity.name)
            for word in words:
                word_lower = word.lower()
                self.vocabulary[word_lower].append(entity.full_name)
                self.word_frequency[word_lower] += 1

            # Also index class name if present
            if entity.class_name:
                class_words = self._extract_words(entity.class_name)
                for word in class_words:
                    word_lower = word.lower()
                    self.vocabulary[word_lower].append(entity.full_name)

            # Index from docstring keywords
            if entity.docstring:
                doc_words = self._extract_words(entity.docstring)
                for word in doc_words[:10]:  # Limit to avoid noise
                    word_lower = word.lower()
                    if len(word_lower) > 3:  # Skip short words
                        self.vocabulary[word_lower].append(entity.full_name)

    def get_code_patterns(self, ui_text: str) -> list[str]:
        """Get code patterns matching UI text from learned vocabulary."""
        ui_words = self._extract_words(ui_text)
        matches = []
        seen = set()

        for word in ui_words:
            word_lower = word.lower()

            # Direct match
            if word_lower in self.vocabulary:
                for full_name in self.vocabulary[word_lower]:
                    if full_name not in seen:
                        matches.append(full_name)
                        seen.add(full_name)

            # Partial match (word is substring of vocabulary word)
            for vocab_word, full_names in self.vocabulary.items():
                if word_lower in vocab_word or vocab_word in word_lower:
                    for full_name in full_names:
                        if full_name not in seen:
                            matches.append(full_name)
                            seen.add(full_name)

        # Sort by frequency (more common patterns first)
        matches.sort(key=lambda x: -self._get_match_score(x, ui_words))

        return matches[:50]  # Limit results

    def suggest_file_patterns(self, app_section: str) -> list[str]:
        """Suggest file patterns based on learned directory structure."""
        patterns = set()
        section_lower = app_section.lower()

        for full_name, entity in self.name_to_entity.items():
            # Check if entity path contains the section
            if section_lower in entity.file_path.lower():
                # Extract directory pattern
                parts = entity.file_path.split("/")
                for i, part in enumerate(parts[:-1]):  # Exclude filename
                    if section_lower in part.lower():
                        pattern = "/".join(parts[:i+1]) + "/*"
                        patterns.add(pattern)

        if not patterns:
            # Fallback to generic patterns
            patterns.add(f"*{section_lower}*")
            patterns.add(f"*/{section_lower}/*")

        return list(patterns)

    def build_search_context(self, extracted: dict) -> dict:
        """Build search context from extracted image data."""
        code_patterns = []
        file_patterns = []

        # From UI elements
        for elem in extracted.get("ui_elements", []):
            code_patterns.extend(self.get_code_patterns(elem))

        # From app section
        if extracted.get("app_section"):
            code_patterns.extend(self.get_code_patterns(extracted["app_section"]))
            file_patterns.extend(self.suggest_file_patterns(extracted["app_section"]))

        # From keywords
        for kw in extracted.get("keywords", []):
            code_patterns.extend(self.get_code_patterns(kw))

        # From error message
        if extracted.get("error_message"):
            code_patterns.extend(self.get_code_patterns(extracted["error_message"]))

        # Dedupe while preserving order
        seen = set()
        unique_patterns = []
        for p in code_patterns:
            if p not in seen:
                unique_patterns.append(p)
                seen.add(p)

        return {
            "code_patterns": unique_patterns,
            "file_patterns": list(set(file_patterns)),
            "error_text": extracted.get("error_message", ""),
            "context": extracted.get("user_action", "")
        }

    def _extract_words(self, text: str) -> list[str]:
        """Extract words from text, handling camelCase and snake_case."""
        if not text:
            return []

        # Split camelCase: "processPayment" -> ["process", "Payment"]
        text = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)

        # Split snake_case: "process_payment" -> ["process", "payment"]
        text = text.replace("_", " ")

        # Extract words (alphanumeric only)
        words = re.findall(r'[a-zA-Z]+', text)

        return [w for w in words if len(w) > 2]  # Skip very short words

    def _get_match_score(self, full_name: str, ui_words: list[str]) -> float:
        """Score a match based on word overlap."""
        name_words = set(w.lower() for w in self._extract_words(full_name))
        ui_words_lower = set(w.lower() for w in ui_words)

        if not name_words:
            return 0

        overlap = len(name_words & ui_words_lower)
        return overlap / len(name_words)

    def get_stats(self) -> dict:
        """Get vocabulary statistics."""
        return {
            "total_entities": len(self.name_to_entity),
            "vocabulary_size": len(self.vocabulary),
            "top_words": sorted(
                self.word_frequency.items(),
                key=lambda x: -x[1]
            )[:20]
        }
