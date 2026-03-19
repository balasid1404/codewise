from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class EntityType(Enum):
    FUNCTION = "function"
    METHOD = "method"
    CLASS = "class"
    ENUM = "enum"
    FIELD = "field"


@dataclass
class CodeEntity:
    id: str
    name: str
    entity_type: EntityType
    file_path: str
    start_line: int
    end_line: int
    signature: str
    body: str
    class_name: Optional[str] = None
    package: Optional[str] = None
    docstring: Optional[str] = None
    embedding: Optional[list[float]] = None
    calls: list[str] = field(default_factory=list)  # methods this entity calls (raw names)
    called_by: list[str] = field(default_factory=list)  # methods that call this
    imports: list[str] = field(default_factory=list)  # import statements in this file
    annotations: list[str] = field(default_factory=list)  # decorators/annotations on this entity
    namespace: Optional[str] = None  # org/team/repo scope for search isolation
    resolved_calls: list[str] = field(default_factory=list)  # resolved entity IDs this calls
    base_classes: list[str] = field(default_factory=list)  # parent classes (inheritance chain)
    file_imports: list[str] = field(default_factory=list)  # file paths this file imports from
    references: list[str] = field(default_factory=list)  # constants/fields/types referenced in body

    @property
    def full_name(self) -> str:
        parts = []
        if self.package:
            parts.append(self.package)
        if self.class_name:
            parts.append(self.class_name)
        parts.append(self.name)
        return ".".join(parts)

    def to_search_text(self) -> str:
        """Rich text for BM25 indexing — includes path, qualified name, annotations."""
        parts = []
        # File path gives module context
        if self.file_path:
            parts.append(self.file_path)
        # Fully qualified name
        parts.append(self.full_name)
        # Annotations/decorators (e.g. @RequestMapping, @Component)
        if self.annotations:
            parts.extend(self.annotations)
        # Signature and name
        parts.append(self.signature)
        parts.append(self.name)
        if self.docstring:
            parts.append(self.docstring)
        if self.class_name:
            parts.append(self.class_name)
        # For fields/enums, include body (the value IS the identity)
        if self.entity_type.value in ("field", "enum") and self.body:
            parts.append(self.body[:500])
        # Include referenced constants/fields for BM25 discoverability
        if self.references:
            parts.extend(self.references)
        return " ".join(parts)

    def to_embedding_text(self) -> str:
        """Rich text for CodeBERT embedding — file path + qualified name + annotations + signature + body."""
        parts = []
        if self.file_path:
            parts.append(self.file_path)
        parts.append(self.full_name)
        if self.annotations:
            parts.extend(self.annotations)
        parts.append(self.signature)
        if self.body:
            parts.append(self.body[:1000])
        return "\n".join(parts)

    def to_embedding_chunks(self, chunk_size: int = 512, overlap: int = 128) -> list[str]:
        """Split entity into overlapping chunks for chunk-level embeddings.

        For large methods, a single embedding loses detail. This produces
        multiple chunks so each part of the body gets its own embedding.
        The header (path + name + signature) is prepended to every chunk
        so each chunk retains context about what entity it belongs to.

        Returns list of text chunks. Short entities return a single chunk.
        """
        header_parts = []
        if self.file_path:
            header_parts.append(self.file_path)
        header_parts.append(self.full_name)
        if self.annotations:
            header_parts.extend(self.annotations[:3])
        header_parts.append(self.signature)
        header = "\n".join(header_parts)

        body = self.body or ""
        if len(body) <= chunk_size:
            return [f"{header}\n{body}"]

        chunks = []
        start = 0
        while start < len(body):
            end = start + chunk_size
            chunk_text = body[start:end]
            chunks.append(f"{header}\n{chunk_text}")
            start += chunk_size - overlap
            if start >= len(body):
                break

        return chunks if chunks else [header]
