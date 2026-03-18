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
            annotations=decorators
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
