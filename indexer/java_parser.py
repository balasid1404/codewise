import hashlib
from pathlib import Path
import javalang
from .entities import CodeEntity, EntityType


class JavaParser:
    def parse_file(self, file_path: Path) -> list[CodeEntity]:
        content = file_path.read_text()
        try:
            tree = javalang.parse.parse(content)
        except javalang.parser.JavaSyntaxError:
            return []

        entities = []
        lines = content.splitlines()
        package = tree.package.name if tree.package else None

        for _, class_node in tree.filter(javalang.tree.ClassDeclaration):
            entities.append(self._extract_class(class_node, file_path, lines, package))

            for method in class_node.methods:
                entities.append(self._extract_method(method, file_path, lines, class_node.name, package))

        return entities

    def _extract_class(self, node, file_path: Path, lines: list[str], package: str | None) -> CodeEntity:
        start_line = node.position.line if node.position else 1
        end_line = self._find_end_line(lines, start_line)
        body = "\n".join(lines[start_line - 1:end_line])

        entity_id = hashlib.md5(f"{file_path}:{node.name}:{start_line}".encode()).hexdigest()

        return CodeEntity(
            id=entity_id,
            name=node.name,
            entity_type=EntityType.CLASS,
            file_path=str(file_path),
            start_line=start_line,
            end_line=end_line,
            signature=f"class {node.name}",
            body=body,
            package=package,
            docstring=node.documentation
        )

    def _extract_method(self, node, file_path: Path, lines: list[str], class_name: str, package: str | None) -> CodeEntity:
        start_line = node.position.line if node.position else 1
        end_line = self._find_end_line(lines, start_line)
        body = "\n".join(lines[start_line - 1:end_line])
        signature = self._get_signature(node)
        calls = self._extract_calls(node)

        entity_id = hashlib.md5(f"{file_path}:{class_name}.{node.name}:{start_line}".encode()).hexdigest()

        return CodeEntity(
            id=entity_id,
            name=node.name,
            entity_type=EntityType.METHOD,
            file_path=str(file_path),
            start_line=start_line,
            end_line=end_line,
            signature=signature,
            body=body,
            class_name=class_name,
            package=package,
            docstring=node.documentation,
            calls=calls
        )

    def _get_signature(self, node) -> str:
        params = []
        if node.parameters:
            for param in node.parameters:
                param_type = param.type.name if param.type else "Object"
                params.append(f"{param_type} {param.name}")
        return_type = node.return_type.name if node.return_type else "void"
        return f"{return_type} {node.name}({', '.join(params)})"

    def _extract_calls(self, node) -> list[str]:
        calls = []
        if node.body:
            for statement in node.body:
                self._find_method_calls(statement, calls)
        return list(set(calls))

    def _find_method_calls(self, node, calls: list[str]) -> None:
        if node is None:
            return
        if isinstance(node, javalang.tree.MethodInvocation):
            calls.append(node.member)
        if hasattr(node, 'children'):
            for child in node.children:
                if isinstance(child, list):
                    for item in child:
                        self._find_method_calls(item, calls)
                elif isinstance(child, javalang.tree.Node):
                    self._find_method_calls(child, calls)

    def _find_end_line(self, lines: list[str], start_line: int) -> int:
        brace_count = 0
        started = False
        for i, line in enumerate(lines[start_line - 1:], start=start_line):
            brace_count += line.count('{') - line.count('}')
            if '{' in line:
                started = True
            if started and brace_count == 0:
                return i
        return len(lines)
