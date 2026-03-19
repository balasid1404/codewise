"""Cross-file relationship resolution for indexed code entities.

Resolves raw call names to entity IDs, inheritance chains,
and file-level import graphs. Optimized for large codebases (5k+ entities).
"""

import os
import re
import time
import logging
from collections import defaultdict

from indexer.entities import CodeEntity

logger = logging.getLogger(__name__)


class RelationshipResolver:

    def resolve(self, entities: list[CodeEntity]) -> list[CodeEntity]:
        """Main entry point. Mutates entities in-place."""
        t_total = time.monotonic()

        t0 = time.monotonic()
        by_name = self._build_name_index(entities)
        logger.info(f"[TIMING] _build_name_index: {time.monotonic()-t0:.3f}s ({len(by_name)} names)")

        t0 = time.monotonic()
        by_file = self._build_file_index(entities)
        logger.info(f"[TIMING] _build_file_index: {time.monotonic()-t0:.3f}s ({len(by_file)} files)")

        t0 = time.monotonic()
        file_name_cache = {fp: {e.name: e for e in ents} for fp, ents in by_file.items()}
        logger.info(f"[TIMING] file_name_cache: {time.monotonic()-t0:.3f}s")

        t0 = time.monotonic()
        module_entities = self._build_module_entity_index(entities, by_file)
        logger.info(f"[TIMING] _build_module_entity_index: {time.monotonic()-t0:.3f}s ({len(module_entities)} module keys)")

        t0 = time.monotonic()
        file_import_scope = self._build_file_import_scopes(entities, by_file, by_name, module_entities)
        logger.info(f"[TIMING] _build_file_import_scopes: {time.monotonic()-t0:.3f}s ({len(file_import_scope)} files)")

        t0 = time.monotonic()
        self._resolve_calls(entities, by_name, file_name_cache, file_import_scope)
        logger.info(f"[TIMING] _resolve_calls: {time.monotonic()-t0:.3f}s")

        t0 = time.monotonic()
        self._resolve_inheritance(entities)
        logger.info(f"[TIMING] _resolve_inheritance: {time.monotonic()-t0:.3f}s")

        t0 = time.monotonic()
        self._resolve_file_imports(entities, by_file)
        logger.info(f"[TIMING] _resolve_file_imports: {time.monotonic()-t0:.3f}s")

        t0 = time.monotonic()
        self._resolve_reference_edges(entities, by_name)
        logger.info(f"[TIMING] _resolve_reference_edges: {time.monotonic()-t0:.3f}s")

        logger.info(f"[TIMING] resolve() TOTAL: {time.monotonic()-t_total:.3f}s for {len(entities)} entities")
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
        # Log module_entities size distribution to detect broad keys
        if module_entities:
            sizes = sorted(((k, len(v)) for k, v in module_entities.items()), key=lambda x: -x[1])
            top5 = sizes[:5]
            logger.info(f"[TIMING] module_entities top-5 keys by size: {top5}")
            total_entries = sum(len(v) for v in module_entities.values())
            logger.info(f"[TIMING] module_entities total entries across all keys: {total_entries}")

        # Collect unique imports per file
        file_imports: dict[str, set[str]] = {}
        for e in entities:
            if e.file_path:
                if e.file_path not in file_imports:
                    file_imports[e.file_path] = set()
                if e.imports:
                    file_imports[e.file_path].update(e.imports)

        total_imports = sum(len(v) for v in file_imports.values())
        logger.info(f"[TIMING] _build_file_import_scopes: {len(file_imports)} files, {total_imports} total imports to resolve")

        file_scopes: dict[str, dict[str, CodeEntity]] = {}
        module_hits = 0
        module_entity_copies = 0

        for fp, imports in file_imports.items():
            scope: dict[str, CodeEntity] = {}
            for imp in imports:
                parts = imp.split(".")

                # Check all suffixes: "a.b.c" → "a.b.c", "b.c", "c"
                for i in range(len(parts)):
                    module_key = ".".join(parts[i:])
                    if module_key in module_entities:
                        module_hits += 1
                        ents_map = module_entities[module_key]
                        module_entity_copies += len(ents_map)
                        for name, ent in ents_map.items():
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

        logger.info(f"[TIMING] _build_file_import_scopes done: {module_hits} module hits, {module_entity_copies} entity copies iterated")
        return file_scopes

    def _resolve_calls(
        self,
        entities: list[CodeEntity],
        by_name: dict[str, list[CodeEntity]],
        file_name_cache: dict[str, dict[str, CodeEntity]],
        file_import_scope: dict[str, dict[str, CodeEntity]],
    ) -> None:
        """Resolve raw call names to entity IDs. All lookups are O(1) dict hits."""
        total_calls = 0
        total_resolved = 0
        closest_path_calls = 0
        for entity in entities:
            if not entity.calls:
                continue

            resolved = []
            same_file = file_name_cache.get(entity.file_path, {})
            import_scope = file_import_scope.get(entity.file_path, {})

            for call_name in entity.calls:
                total_calls += 1
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
                            closest_path_calls += 1
                            target = self._closest_by_path(entity.file_path, candidates)

                if target and target.id != entity.id:
                    resolved.append(target.id)
                    total_resolved += 1

            entity.resolved_calls = resolved

        logger.info(f"[TIMING] _resolve_calls stats: {total_calls} calls, {total_resolved} resolved, {closest_path_calls} closest_path lookups")

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

    def _resolve_reference_edges(
        self,
        entities: list[CodeEntity],
        by_name: dict[str, list[CodeEntity]],
    ) -> None:
        """Gap 5: Resolve constant/field references to entity IDs.

        For each entity that has 'references' (UPPER_SNAKE_CASE names found in body),
        check if those names correspond to known entities. If so, add the referenced
        entity's ID to resolved_calls so the graph ranker can traverse these edges.

        This enables: if HAWKFIRE_ALL_DEVICES_ANNUAL_NON_DISCOUNTED is suspicious,
        propagate to all entities that reference it.
        """
        total_refs = 0
        resolved_refs = 0

        for entity in entities:
            if not entity.references:
                continue

            new_resolved = list(entity.resolved_calls)  # preserve existing
            for ref_name in entity.references:
                total_refs += 1
                # Look up by exact name
                candidates = by_name.get(ref_name)
                if candidates:
                    for c in candidates:
                        if c.id != entity.id and c.id not in new_resolved:
                            new_resolved.append(c.id)
                            resolved_refs += 1

            entity.resolved_calls = new_resolved

        logger.info(f"[TIMING] _resolve_reference_edges: {total_refs} refs checked, {resolved_refs} resolved")
