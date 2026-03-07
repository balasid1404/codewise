"""
Multi-repo indexer - index multiple packages/repos and search across all.

Use case: Enterprise codebase with multiple packages like:
  - DigitalMusicSubsCommon
  - DigitalMusicSubsWeb
  - PaymentService
  - AuthService
"""

from pathlib import Path
from dataclasses import dataclass
from typing import Optional

from .code_indexer import CodeIndexer
from .entities import CodeEntity
from .local_cache import LocalIndexCache


@dataclass
class RepoInfo:
    """Metadata about an indexed repo."""
    name: str
    path: str
    entity_count: int
    package_prefix: Optional[str] = None


class MultiRepoIndexer:
    """
    Index and search across multiple repositories/packages.
    
    Usage:
        indexer = MultiRepoIndexer()
        indexer.add_repo("/path/to/repo1", name="auth-service")
        indexer.add_repo("/path/to/repo2", name="payment-service")
        indexer.index_all()
        
        results = indexer.search("payment validation", top_k=10)
    """
    
    def __init__(self):
        self.repos: dict[str, RepoInfo] = {}
        self.indexer = CodeIndexer()
        self.cache = LocalIndexCache()
        self._indexed = False
    
    def add_repo(
        self, 
        path: str, 
        name: Optional[str] = None,
        package_prefix: Optional[str] = None
    ) -> None:
        """
        Add a repository to be indexed.
        
        Args:
            path: Path to the repository
            name: Optional name (defaults to folder name)
            package_prefix: Optional package prefix for filtering (e.g., "com.amazon.payment")
        """
        resolved = Path(path).resolve()
        repo_name = name or resolved.name
        
        self.repos[repo_name] = RepoInfo(
            name=repo_name,
            path=str(resolved),
            entity_count=0,
            package_prefix=package_prefix
        )
    
    def add_repos_from_parent(
        self, 
        parent_path: str,
        exclude: Optional[list[str]] = None
    ) -> int:
        """
        Add all subdirectories as repos.
        
        Args:
            parent_path: Parent directory containing multiple repos
            exclude: List of folder names to exclude
            
        Returns:
            Number of repos added
        """
        exclude = exclude or []
        exclude_set = set(exclude + [".git", "node_modules", "__pycache__", "venv", ".venv"])
        
        parent = Path(parent_path).resolve()
        added = 0
        
        for child in parent.iterdir():
            if child.is_dir() and child.name not in exclude_set:
                # Check if it looks like a code repo
                has_code = (
                    list(child.rglob("*.java"))[:1] or 
                    list(child.rglob("*.py"))[:1]
                )
                if has_code:
                    self.add_repo(str(child))
                    added += 1
        
        return added
    
    def index_all(self, force: bool = False) -> dict[str, int]:
        """
        Index all added repositories.
        
        Args:
            force: Force re-indexing even if cached
            
        Returns:
            Dict of repo_name → entity_count
        """
        results = {}
        all_entities = []
        
        for repo_name, repo_info in self.repos.items():
            # Check cache
            cached = None if force else self.cache.get(repo_info.path)
            
            if cached:
                print(f"[{repo_name}] Using cache ({cached['count']} entities)")
                entities = list(cached["entities"].values())
                repo_info.entity_count = cached["count"]
            else:
                print(f"[{repo_name}] Indexing {repo_info.path}...")
                entities = self.indexer.index_directory(Path(repo_info.path))
                repo_info.entity_count = len(entities)
                
                # Cache it
                self.cache.set(repo_info.path, {
                    "count": len(entities),
                    "entities": {e.id: e for e in entities}
                })
            
            # Tag entities with repo name
            for entity in entities:
                entity.repo = repo_name
            
            all_entities.extend(entities)
            results[repo_name] = repo_info.entity_count
        
        # Build combined retriever
        if all_entities:
            from retrieval import HybridRetriever
            self.retriever = HybridRetriever(all_entities, self.indexer.encoder)
            self._indexed = True
        
        return results
    
    def search(
        self, 
        query: str, 
        top_k: int = 10,
        repo_filter: Optional[str] = None
    ) -> list[tuple[CodeEntity, float, str]]:
        """
        Search across all indexed repos.
        
        Args:
            query: Search query
            top_k: Number of results
            repo_filter: Optional repo name to filter results
            
        Returns:
            List of (entity, score, repo_name) tuples
        """
        if not self._indexed:
            raise RuntimeError("Call index_all() first")
        
        results = self.retriever.search(query, top_k=top_k * 2)
        
        # Add repo info and optionally filter
        enriched = []
        for entity, score in results:
            repo_name = getattr(entity, 'repo', 'unknown')
            
            if repo_filter and repo_name != repo_filter:
                continue
                
            enriched.append((entity, score, repo_name))
        
        return enriched[:top_k]
    
    def get_stats(self) -> dict:
        """Get indexing statistics."""
        total = sum(r.entity_count for r in self.repos.values())
        return {
            "repos": len(self.repos),
            "total_entities": total,
            "by_repo": {name: info.entity_count for name, info in self.repos.items()}
        }
    
    def list_repos(self) -> list[RepoInfo]:
        """List all added repos."""
        return list(self.repos.values())
