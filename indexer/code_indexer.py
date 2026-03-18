from pathlib import Path
from sentence_transformers import SentenceTransformer
from .python_parser import PythonParser
from .java_parser import JavaParser
from .js_ts_parser import JsTsParser
from .html_parser import HtmlParser
from .entities import CodeEntity


class CodeIndexer:
    def __init__(self, model_name: str = "microsoft/unixcoder-base"):
        self.python_parser = PythonParser()
        self.java_parser = JavaParser()
        self.js_ts_parser = JsTsParser()
        self.html_parser = HtmlParser()
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

        for js_file in path.rglob("*.js"):
            if self._should_skip(js_file):
                continue
            entities.extend(self.js_ts_parser.parse_file(js_file))

        for ts_file in path.rglob("*.ts"):
            if self._should_skip(ts_file):
                continue
            entities.extend(self.js_ts_parser.parse_file(ts_file))

        for html_file in path.rglob("*.html"):
            if self._should_skip(html_file):
                continue
            entities.extend(self.html_parser.parse_file(html_file))

        # Generate embeddings (chunk-level for large entities)
        import numpy as np

        for entity in entities:
            chunks = entity.to_embedding_chunks(chunk_size=512, overlap=128)
            if len(chunks) == 1:
                emb = self.encoder.encode(chunks[0], show_progress_bar=False)
                entity.embedding = emb.tolist()
            else:
                chunk_embs = self.encoder.encode(chunks, show_progress_bar=False)
                mean_emb = np.mean(chunk_embs, axis=0)
                entity.embedding = mean_emb.tolist()
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
