from pathlib import Path
from sentence_transformers import SentenceTransformer
from .python_parser import PythonParser
from .java_parser import JavaParser
from .entities import CodeEntity


class CodeIndexer:
    def __init__(self, model_name: str = "microsoft/codebert-base"):
        self.python_parser = PythonParser()
        self.java_parser = JavaParser()
        self.encoder = SentenceTransformer(model_name)
        self.entities: dict[str, CodeEntity] = {}

    def index_directory(self, path: Path) -> list[CodeEntity]:
        entities = []

        for py_file in path.rglob("*.py"):
            if self._should_skip(py_file):
                continue
            entities.extend(self.python_parser.parse_file(py_file))

        for java_file in path.rglob("*.java"):
            if self._should_skip(java_file):
                continue
            entities.extend(self.java_parser.parse_file(java_file))

        # Generate embeddings
        texts = [e.to_search_text() for e in entities]
        if texts:
            embeddings = self.encoder.encode(texts, show_progress_bar=True)
            for entity, emb in zip(entities, embeddings):
                entity.embedding = emb.tolist()
                self.entities[entity.id] = entity

        return entities

    def _should_skip(self, file_path: Path) -> bool:
        skip_dirs = {"venv", "node_modules", ".git", "__pycache__", "build", "dist"}
        return any(part in skip_dirs for part in file_path.parts)

    def get_entity(self, entity_id: str) -> CodeEntity | None:
        return self.entities.get(entity_id)

    def get_entities_by_file(self, file_path: str) -> list[CodeEntity]:
        return [e for e in self.entities.values() if e.file_path == file_path]

    def get_entities_by_name(self, name: str) -> list[CodeEntity]:
        return [e for e in self.entities.values() if e.name == name or e.full_name.endswith(name)]
