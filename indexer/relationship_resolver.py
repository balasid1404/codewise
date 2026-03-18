"""Cross-file relationship resolution for indexed code entities.

Resolves raw call names to entity IDs, inheritance chains,
and file-level import graphs. Optimized for large codebases (5k+ entities).
"""

import os
import re
import logging
from collections import defaultdict

from indexer.entities import CodeEntity

logger = logging.getLogger(__name__)


class RelationshipResolver:

    def resolve(self, entities: list[CodeEntity]) -> list[CodeEntity]:
        """Main entry point. Mutates entities in-place."""
        by_name = self._build_name_index(entities)
        by_file = self._build_file_index(entities)

        # Per-file same-name cache
        file_name_cache = {fp: {e.name: e for e in ents} for fp, ents in by_file.items()}

        # Module stem → {entity_name: entity} for import resolution
        module_entities = self._build_module_entity_index(entities, by_file)

        # Per-file import scope: call_name → entity
        file_import_scope = self._build_file_import_scopes(entities, by_file, by_name, module_entities)

        self._resolve_calls(entities, by_name, file_name_cache, file_import_scope)
        self._resolve_inheritance(entities)
        self._resolve_file_imports(entities, by_file)
        return entities

    def _build_name_index(self, entities: list[CodeEntity]) -> dict[str, list[CodeEntity]]:
        by_name: dict[str, list[CodeEntity]] = defaultdict(list)
        for e in entities:
            by_name[e.name].append(e)
            if e.full_name and e.full_name != e.name:
                by_name[e.full_name].append(e)
        return dict(by_name)

    def _build_file_index(self, entities: list[CodeEntity]) -> dict[str, list[CodeEntity]]:
        by_file: dict[str, list[CodeEntity]] = defaultdict(list)
        for e in entities:
            if e.file_path:
                by_file[e.file_path].append(e)
        return dict(by_file)

    def _build_module_entity_index(
        self, entities: list[CodeEntity], by_file: dict[str, list[CodeEntity]]
    ) -> dict[str, dict[str, CodeEntity]]:
        """Map module stem → {entity_name: entity}.

        For file 'src/payment/service.py', creates entries for:
          'service' → {all entity names in that file}
          'payment.service' → same
          'src.payment.service' → same
        """
        module_entities: dict[str, dict[str, CodeEntity]] = {}
        for fp, ents in by_file.items():
            name_map = {e.name: e for e in ents}
            parts = fp.replace("\\", "/").split("/")
            parts[-1] = os.path.splitext(parts[-1])[0]
            for i in range(len(parts)):
                key = ".".join(parts[i:])
                if key not in module_entities:
                    module_entities[key] = name_map
                # Also register just the filename stem
            stem = parts[-1]
            if stem not in module_entities:
                module_entities[stem] = name_map
        return module_entities

    def _build_file_import_scopes(
        self,
        entities: list[CodeEntity],
        by_file: dict[str, list[CodeEntity]],
        by_name: dict[str, list[CodeEntity]],
        module_entities: dict[str, dict[str, CodeEntity]],
    ) -> dict[str, dict[str, CodeEntity]]:
        """Precompute per-file: call_name → entity reachable via imports.

        For each file's imports, look up the module in module_entities
        and register all its entity names as reachable. O(files × imports).
        """
        # Collect unique imports per file
        file_imports: dict[str, set[str]] = {}
        for e in entities:
            if e.file_path:
                if e.file_path not in file_imports:
                    file_imports[e.file_path] = set()
                if e.imports:
                    file_imports[e.file_path].update(e.imports)

        file_scopes: dict[str, dict[str, CodeEntity]] = {}
        for fp, imports in file_imports.items():
            scope: dict[str, CodeEntity] = {}
            for imp in imports:
                # Try the import as a module stem lookup
                # "from payment.service import X" → imp might be "payment.service" or "X"
                # "import os.path" → imp is "os.path"
                parts = imp.split(".")

                # Check all suffixes: "a.b.c" → "a.b.c", "b.c", "c"
                for i in range(len(parts)):
                    module_key = ".".join(parts[i:])
                    if module_key in module_entities:
                        # Register all entities from that module as reachable
                        for name, ent in module_entities[module_key].items():
                            if name not in scope:
                                scope[name] = ent
                        break

                # Direct name import: "from x import validate" → "validate" in by_name
                last = parts[-1]
                if last not in scope:
                    candidates = by_name.get(last)
                    if candidates:
                        scope[last] = candidates[0]

            file_scopes[fp] = scope
        return file_scopes

    def _resolve_calls(
        self,
        entities: list[CodeEntity],
        by_name: dict[str, list[CodeEntity]],
        file_name_cache: dict[str, dict[str, CodeEntity]],
        file_import_scope: dict[str, dict[str, CodeEntity]],
    ) -> None:
        """Resolve raw call names to entity IDs. All lookups are O(1) dict hits."""
        for entity in entities:
            if not entity.calls:
                continue

            resolved = []
            same_file = file_name_cache.get(entity.file_path, {})
            import_scope = file_import_scope.get(entity.file_path, {})

            for call_name in entity.calls:
                target = None

                # 1. Same-file match
                t = same_file.get(call_name)
                if t and t.id != entity.id:
                    target = t
                # 2. Class.method match
                elif entity.class_name:
                    candidates = by_name.get(f"{entity.class_name}.{call_name}")
                    if candidates:
                        target = candidates[0]
                # 3. Import scope (O(1) lookup)
                if not target:
                    t = import_scope.get(call_name)
                    if t and t.id != entity.id:
                        target = t
                # 4. Global fallback
                if not target:
                    candidates = by_name.get(call_name)
                    if candidates:
                        if len(candidates) == 1:
                            target = candidates[0]
                        elif entity.file_path:
                            target = self._closest_by_path(entity.file_path, candidates)

                if target and target.id != entity.id:
                    resolved.append(target.id)

            entity.resolved_calls = resolved

    def _closest_by_path(self, source_path: str, candidates: list[CodeEntity]) -> CodeEntity:
        source_dir = os.path.dirname(source_path)
        def dist(e):
            e_dir = os.path.dirname(e.file_path) if e.file_path else ""
            common = os.path.commonpath([source_dir, e_dir]) if source_dir and e_dir else ""
            return len(source_dir) + len(e_dir) - 2 * len(common)
        return min(candidates, key=dist)

    def _resolve_inheritance(self, entities: list[CodeEntity]) -> None:
        """Extract base classes from class signatures."""
        for entity in entities:
            if entity.entity_type.value != "class":
                continue
            sig = entity.signature
            bases = []
            m = re.search(r"class\s+\w+\s*\(([^)]+)\)", sig)
            if m:
                bases = [b.strip() for b in m.group(1).split(",") if b.strip()]
            if not bases:
                m = re.search(r"extends\s+([\w.]+)", sig)
                if m:
                    bases.append(m.group(1))
                m = re.search(r"implements\s+([\w.,\s]+)", sig)
                if m:
                    bases.extend(b.strip() for b in m.group(1).split(",") if b.strip())
            entity.base_classes = bases

    def _resolve_file_imports(
        self,
        entities: list[CodeEntity],
        by_file: dict[str, list[CodeEntity]],
    ) -> None:
        """Resolve import strings to actual file paths."""
        all_files = set(by_file.keys())

        # stem/module → file path
        stem_to_file: dict[str, str] = {}
        for fp in all_files:
            parts = fp.replace("\\", "/").split("/")
            parts[-1] = os.path.splitext(parts[-1])[0]
            stem_to_file.setdefault(parts[-1], fp)
            for i in range(len(parts)):
                stem_to_file.setdefault(".".join(parts[i:]), fp)

        # Collect imports per file
        file_to_imports: dict[str, list[str]] = {}
        for e in entities:
            if e.file_path and e.file_path not in file_to_imports:
                file_to_imports[e.file_path] = list(e.imports) if e.imports else []

        # Resolve per file
        file_resolved: dict[str, list[str]] = {}
        for fp, imports in file_to_imports.items():
            resolved = set()
            base_dir = os.path.dirname(fp)
            for imp in imports:
                if imp.startswith("@"):
                    continue
                imp_clean = imp.replace("/", ".").replace("\\", ".").lstrip(".")
                for candidate in (imp_clean, imp_clean.split(".")[-1]):
                    if candidate in stem_to_file:
                        r = stem_to_file[candidate]
                        if r != fp:
                            resolved.add(r)
                        break
                else:
                    if imp.startswith("."):
                        rel = imp.replace(".", "/", 1) if imp.startswith("./") else imp
                        for ext in ("", ".py", ".js", ".ts", ".java"):
                            cp = os.path.normpath(os.path.join(base_dir, rel + ext))
                            if cp in all_files:
                                resolved.add(cp)
                                break
            file_resolved[fp] = list(resolved)

        for e in entities:
            e.file_imports = file_resolved.get(e.file_path, [])
