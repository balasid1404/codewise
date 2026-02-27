from abc import ABC, abstractmethod
from indexer.entities import CodeEntity


class VectorStore(ABC):
    @abstractmethod
    def index(self, entities: list[CodeEntity]) -> int:
        """Index entities. Returns count indexed."""
        pass

    @abstractmethod
    def search_bm25(self, query: str, top_k: int = 100) -> list[tuple[CodeEntity, float]]:
        """BM25 text search."""
        pass

    @abstractmethod
    def search_vector(self, embedding: list[float], top_k: int = 20) -> list[tuple[CodeEntity, float]]:
        """Vector similarity search."""
        pass

    @abstractmethod
    def search_hybrid(self, query: str, embedding: list[float], top_k: int = 20) -> list[tuple[CodeEntity, float]]:
        """Combined BM25 + vector search."""
        pass

    @abstractmethod
    def get_by_file(self, file_path: str) -> list[CodeEntity]:
        """Get entities by file path."""
        pass

    @abstractmethod
    def get_by_name(self, name: str) -> list[CodeEntity]:
        """Get entities by method/class name."""
        pass
