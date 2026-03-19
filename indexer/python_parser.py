import ast
import hashlib
from pathlib import Path
from .entities import CodeEntity, EntityType


class PythonParser:
    def parse_file(self, file_path: Path) -> list[CodeEntity]:
        content = file_path.read_text()
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return []

        entities = []
        lines = content.splitlines()

        # Extract file-level imports
        file_imports = self._extract_imports(tree)

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                ent = self._extract_function(node, file_path, lines, None)
                ent.imports = file_imports
                entities.append(ent)
            elif isinstance(node, ast.ClassDef):
                ent = self._extract_class(node, file_path, lines)
                ent.imports = file_imports
                entities.append(ent)
                for item in node.body:
                    if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                        ent = self._extract_function(item, file_path, lines, node.name)
                        ent.imports = file_imports
                        entities.append(ent)
                    elif isinstance(item, ast.Assign | ast.AnnAssign):
                        # Class-level attributes / constants
                        for ent in self._extract_assignment(item, file_path, lines, node.name):
                            ent.imports = file_imports
                            entities.append(ent)

        # Module-level constants (top-level assignments with UPPER_CASE names)
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.Assign | ast.AnnAssign):
                for ent in self._extract_assignment(node, file_path, lines, None):
                    ent.imports = file_imports
                    entities.append(ent)

        return entities

    def _extract_imports(self, tree: ast.Module) -> list[str]:
        """Extract import statements from the module."""
        imports = []
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    imports.append(f"{module}.{alias.name}" if module else alias.name)
        return imports

    def _extract_function(self, node: ast.FunctionDef, file_path: Path, lines: list[str], class_name: str | None) -> CodeEntity:
        body = "\n".join(lines[node.lineno - 1:node.end_lineno])
        signature = self._get_signature(node)
        docstring = ast.get_docstring(node)
        calls = self._extract_calls(node)
        decorators = [f"@{ast.unparse(d)}" for d in node.decorator_list]

        entity_id = hashlib.md5(f"{file_path}:{node.name}:{node.lineno}".encode()).hexdigest()

        return CodeEntity(
            id=entity_id,
            name=node.name,
            entity_type=EntityType.METHOD if class_name else EntityType.FUNCTION,
            file_path=str(file_path),
            start_line=node.lineno,
            end_line=node.end_lineno or node.lineno,
            signature=signature,
            body=body,
            class_name=class_name,
            docstring=docstring,
            calls=calls,
            annotations=decorators
        )

    def _extract_class(self, node: ast.ClassDef, file_path: Path, lines: list[str]) -> CodeEntity:
        body = "\n".join(lines[node.lineno - 1:node.end_lineno])
        docstring = ast.get_docstring(node)
        decorators = [f"@{ast.unparse(d)}" for d in node.decorator_list]
        bases = [ast.unparse(b) for b in node.bases]
        sig = f"class {node.name}" + (f"({', '.join(bases)})" if bases else "")
        entity_id = hashlib.md5(f"{file_path}:{node.name}:{node.lineno}".encode()).hexdigest()

        return CodeEntity(
            id=entity_id,
            name=node.name,
            entity_type=EntityType.CLASS,
            file_path=str(file_path),
            start_line=node.lineno,
            end_line=node.end_lineno or node.lineno,
            signature=sig,
            body=body,
            docstring=docstring,
            annotations=decorators,
            base_classes=bases
        )

    def _get_signature(self, node: ast.FunctionDef) -> str:
        args = []
        for arg in node.args.args:
            annotation = ""
            if arg.annotation:
                annotation = f": {ast.unparse(arg.annotation)}"
            args.append(f"{arg.arg}{annotation}")
        return f"def {node.name}({', '.join(args)})"

    def _extract_calls(self, node: ast.FunctionDef) -> list[str]:
        calls = []
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                if isinstance(child.func, ast.Name):
                    calls.append(child.func.id)
                elif isinstance(child.func, ast.Attribute):
                    calls.append(child.func.attr)
        return list(set(calls))

    def _extract_assignment(self, node, file_path: Path, lines: list[str], class_name: str | None) -> list[CodeEntity]:
        """Extract constants and class attributes from assignments.

        Captures:
        - Module-level: MAX_RETRIES = 3, DEFAULT_CONFIG = {...}
        - Class-level: STATUS_ACTIVE = "active", enum members
        - Annotated: name: str = "value"

        Only extracts UPPER_CASE names at module level (conventions for constants).
        At class level, extracts all assignments (class attributes matter for search).
        """
        entities = []
        names = []

        if isinstance(node, ast.AnnAssign) and node.target:
            if isinstance(node.target, ast.Name):
                names.append(node.target.id)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.append(target.id)

        for name in names:
            # Module-level: only UPPER_CASE (constants)
            # Class-level: all assignments (attributes, enum members, etc.)
            if class_name is None and not name.isupper() and not name.upper() == name:
                # Skip non-constant module-level assignments like `logger = ...`
                if not any(c == '_' for c in name) or not name[0].isupper():
                    continue

            start_line = node.lineno
            end_line = node.end_lineno or start_line
            body = "\n".join(lines[start_line - 1:end_line])

            # Build signature
            if isinstance(node, ast.AnnAssign) and node.annotation:
                ann = ast.unparse(node.annotation)
                sig = f"{name}: {ann}"
            else:
                sig = f"{name} = ..."

            entity_id = hashlib.md5(f"{file_path}:{name}:{start_line}".encode()).hexdigest()

            entities.append(CodeEntity(
                id=entity_id,
                name=name,
                entity_type=EntityType.FIELD,
                file_path=str(file_path),
                start_line=start_line,
                end_line=end_line,
                signature=sig,
                body=body,
                class_name=class_name,
            ))

        return entities
