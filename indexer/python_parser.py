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

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                entities.append(self._extract_function(node, file_path, lines, None))
            elif isinstance(node, ast.ClassDef):
                entities.append(self._extract_class(node, file_path, lines))
                for item in node.body:
                    if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                        entities.append(self._extract_function(item, file_path, lines, node.name))

        return entities

    def _extract_function(self, node: ast.FunctionDef, file_path: Path, lines: list[str], class_name: str | None) -> CodeEntity:
        body = "\n".join(lines[node.lineno - 1:node.end_lineno])
        signature = self._get_signature(node)
        docstring = ast.get_docstring(node)
        calls = self._extract_calls(node)

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
            calls=calls
        )

    def _extract_class(self, node: ast.ClassDef, file_path: Path, lines: list[str]) -> CodeEntity:
        body = "\n".join(lines[node.lineno - 1:node.end_lineno])
        docstring = ast.get_docstring(node)
        entity_id = hashlib.md5(f"{file_path}:{node.name}:{node.lineno}".encode()).hexdigest()

        return CodeEntity(
            id=entity_id,
            name=node.name,
            entity_type=EntityType.CLASS,
            file_path=str(file_path),
            start_line=node.lineno,
            end_line=node.end_lineno or node.lineno,
            signature=f"class {node.name}",
            body=body,
            docstring=docstring
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
