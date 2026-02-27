from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class EntityType(Enum):
    FUNCTION = "function"
    METHOD = "method"
    CLASS = "class"


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
    calls: list[str] = field(default_factory=list)  # methods this entity calls
    called_by: list[str] = field(default_factory=list)  # methods that call this

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
        """Text representation for BM25 indexing."""
        parts = [self.signature, self.name]
        if self.docstring:
            parts.append(self.docstring)
        if self.class_name:
            parts.append(self.class_name)
        return " ".join(parts)
