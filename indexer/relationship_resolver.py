"""Post-parse relationship resolution for cross-file call graphs.

After all files are parsed, this module:
1. Resolves raw call names to actual entity IDs (cross-file)
2. Builds inheritance chains (base_classes → child methods inherit parent's callers)
3. Builds file-level import graph (file A imports from file B)

This runs as a single pass over all entities before embedding/indexing.
"""

import logging
import os
from collections import defaultdict
from .entities import CodeEntity

logger = logging.getLogger(__name__)


class RelationshipResolver:
    """Resolve cross-file relationships between parsed entities."""

    def resolve(self, entities: list[CodeEntity]) -> list[CodeEntity]:
        """
        Main entry point. Mutates entities in-place with:
        - resolved_calls: list of entity IDs (instead of raw names)
        - base_classes: inheritance chain
        - file_imports: resolved file paths this file depends on

        Returns the same list (mutated).
        """
        # Build lookup indexes
        by_name = self._build_name_index(entities)
        by_file = self._build_file_index(entities)
        by_id = {e.id: e for e in entities}
        file_to_imports = self._build_file_import_map(entities)

        # 1. Resolve calls to entity IDs
        self._resolve_calls(entities, by_name, by_file)

        # 2. Resolve inheritance chains
        self._resolve_inheritance(entities, by_name)

        # 3. Build file-level import graph
        self._resolve_file_imports(entities, by_file, file_to_imports)

        return entities

    def _build_name_index(self, entities: list[CodeEntity]) -> dict[str, list[CodeEntity]]:
        """Build name → entities index for call resolution.

        Indexes by: short name, full_name, class.method, and module.name patterns.
        """
        idx: dict[str, list[CodeEntity]] = defaultdict(list)
        for e in entities:
            idx[e.name].append(e)
            idx[e.full_name].append(e)
            if e.class_name:
                idx[f"{e.class_name}.{e.name}"].append(e)
            # Module-level: filename stem + name (e.g. "utils.validate")
            if e.file_path:
                stem = os.path.splitext(os.path.basename(e.file_path))[0]
                idx[f"{stem}.{e.name}"].append(e)
        return idx

    def _build_file_index(self, entities: list[CodeEntity]) -> dict[str, list[CodeEntity]]:
        """Build file_path → entities index."""
        idx: dict[str, list[CodeEntity]] = defaultdict(list)
        for e in entities:
            if e.file_path:
                idx[e.file_path].append(e)
        return idx

    def _build_file_import_map(self, entities: list[CodeEntity]) -> dict[str, list[str]]:
        """Build file_path → list of import strings (from the first entity per file)."""
        seen = set()
        result: dict[str, list[str]] = {}
        for e in entities:
            if e.file_path and e.file_path not in seen:
                seen.add(e.file_path)
                result[e.file_path] = e.imports
        return result

    def _resolve_calls(
        self,
        entities: list[CodeEntity],
        by_name: dict[str, list[CodeEntity]],
        by_file: dict[str, list[CodeEntity]],
    ) -> None:
        """Resolve raw call names to entity IDs.

        Resolution priority:
        1. Same-file match (most likely correct)
        2. Same-class match
        3. Import-scoped match (if the call target is in an imported module)
        4. Global name match (fallback, may be ambiguous)
        """
        for entity in entities:
            resolved = []
            same_file_entities = by_file.get(entity.file_path, [])
            same_file_names = {e.name: e for e in same_file_entities if e.id != entity.id}

            for call_name in entity.calls:
                target = None

                # 1. Same-file match
                if call_name in same_file_names:
                    target = same_file_names[call_name]
                # 2. Class.method match
                elif entity.class_name and f"{entity.class_name}.{call_name}" in by_name:
                    candidates = by_name[f"{entity.class_name}.{call_name}"]
                    target = candidates[0] if candidates else None
                # 3. Import-scoped: check if call matches an imported module's entity
                elif not target:
                    target = self._resolve_via_imports(
                        call_name, entity.imports, by_name
                    )
                # 4. Global fallback (pick closest by file path if ambiguous)
                if not target and call_name in by_name:
                    candidates = by_name[call_name]
                    if len(candidates) == 1:
                        target = candidates[0]
                    elif entity.file_path:
                        # Prefer same directory
                        target = self._closest_by_path(entity.file_path, candidates)

                if target and target.id != entity.id:
                    resolved.append(target.id)

            entity.resolved_calls = resolved

    def _resolve_via_imports(
        self, call_name: str, imports: list[str], by_name: dict[str, list[CodeEntity]]
    ) -> CodeEntity | None:
        """Try to resolve a call via the file's imports.

        If imports contain 'payment.service' and call is 'validate',
        try 'service.validate' and 'payment.service.validate'.
        """
        for imp in imports:
            # Try module.call_name patterns
            parts = imp.split(".")
            for i in range(len(parts)):
                key = ".".join(parts[i:]) + f".{call_name}"
                if key in by_name:
                    return by_name[key][0]
            # Direct import: if import is 'validate' itself
            if parts[-1] == call_name and call_name in by_name:
                return by_name[call_name][0]
        return None

    def _closest_by_path(self, source_path: str, candidates: list[CodeEntity]) -> CodeEntity:
        """Pick the candidate closest to source by directory path."""
        source_dir = os.path.dirname(source_path)

        def path_distance(e: CodeEntity) -> int:
            e_dir = os.path.dirname(e.file_path) if e.file_path else ""
            # Count common prefix length
            common = os.path.commonpath([source_dir, e_dir]) if source_dir and e_dir else ""
            return len(source_dir) + len(e_dir) - 2 * len(common)

        return min(candidates, key=path_distance)

    def _resolve_inheritance(
        self, entities: list[CodeEntity], by_name: dict[str, list[CodeEntity]]
    ) -> None:
        """Extract and resolve base classes from class signatures.

        Parses 'class Foo(Bar, Baz)' or 'class Foo extends Bar implements Baz'
        and stores resolved base class names.
        """
        import re

        class_entities = [e for e in entities if e.entity_type.value == "class"]

        for entity in class_entities:
            sig = entity.signature
            bases = []

            # Python: class Foo(Bar, Baz)
            m = re.search(r"class\s+\w+\s*\(([^)]+)\)", sig)
            if m:
                bases = [b.strip() for b in m.group(1).split(",") if b.strip()]

            # Java/TS: class Foo extends Bar implements Baz, Qux
            if not bases:
                m = re.search(r"extends\s+([\w.]+)", sig)
                if m:
                    bases.append(m.group(1))
                m = re.search(r"implements\s+([\w.,\s]+)", sig)
                if m:
                    bases.extend(b.strip() for b in m.group(1).split(",") if b.strip())

            entity.base_classes = bases

            # Propagate: methods of this class inherit parent's base_classes context
            # (stored on the class entity, methods can look up their class's base_classes)

    def _resolve_file_imports(
        self,
        entities: list[CodeEntity],
        by_file: dict[str, list[CodeEntity]],
        file_to_imports: dict[str, list[str]],
    ) -> None:
        """Resolve import strings to actual file paths in the codebase.

        Maps 'from payment.service import validate' → 'src/payment/service.py'
        Maps './utils' → 'src/utils.js' or 'src/utils.ts'
        """
        # Build a lookup: module_name → file_path
        all_files = set(by_file.keys())
        stem_to_file: dict[str, str] = {}
        for fp in all_files:
            stem = os.path.splitext(os.path.basename(fp))[0]
            stem_to_file[stem] = fp
            # Also index by dotted module path: src/payment/service.py → payment.service
            parts = fp.replace("\\", "/").split("/")
            # Remove extension from last part
            parts[-1] = os.path.splitext(parts[-1])[0]
            for i in range(len(parts)):
                module_key = ".".join(parts[i:])
                stem_to_file[module_key] = fp

        seen_files = set()
        for entity in entities:
            fp = entity.file_path
            if not fp or fp in seen_files:
                if fp in seen_files:
                    # Copy file_imports from first entity of same file
                    for other in by_file.get(fp, []):
                        if other.file_imports:
                            entity.file_imports = other.file_imports
                            break
                continue
            seen_files.add(fp)

            imports = file_to_imports.get(fp, [])
            resolved_files = set()

            for imp in imports:
                # Try direct module name match
                imp_clean = imp.replace("/", ".").replace("\\", ".").lstrip(".")
                # Remove leading @ for scoped packages
                if imp_clean.startswith("@"):
                    continue  # skip npm scoped packages

                # Try various resolutions
                for candidate in [imp_clean, imp_clean.split(".")[-1]]:
                    if candidate in stem_to_file:
                        resolved = stem_to_file[candidate]
                        if resolved != fp:
                            resolved_files.add(resolved)
                        break

                # For relative imports like ./utils or ../common/helpers
                if imp.startswith("."):
                    base_dir = os.path.dirname(fp)
                    rel = imp.replace(".", "/", 1) if imp.startswith("./") else imp
                    # Try with common extensions
                    for ext in ("", ".py", ".js", ".ts", ".java"):
                        candidate_path = os.path.normpath(os.path.join(base_dir, rel + ext))
                        if candidate_path in all_files:
                            resolved_files.add(candidate_path)
                            break

            file_imports_list = list(resolved_files)
            # Set on all entities from this file
            for e in by_file.get(fp, []):
                e.file_imports = file_imports_list
