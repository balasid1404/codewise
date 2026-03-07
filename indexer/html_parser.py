"""HTML parser using tree-sitter — extracts page entities and inline scripts."""

import re
import hashlib
from pathlib import Path
from tree_sitter_languages import get_parser
from .entities import CodeEntity, EntityType


class HtmlParser:
    """Parse HTML files to extract page components and inline JS functions."""

    def __init__(self):
        self._html_parser = get_parser("html")
        self._js_parser = get_parser("javascript")

    def parse_file(self, file_path: Path) -> list[CodeEntity]:
        try:
            content = file_path.read_text(errors="ignore")
            source = content.encode("utf-8")
        except Exception:
            return []

        tree = self._html_parser.parse(source)
        entities = []
        lines = content.splitlines()

        # Extract the page as a single entity for UI mapping
        title = self._extract_title(tree.root_node, source)
        page_name = file_path.stem
        eid = hashlib.md5(f"{file_path}:page:{page_name}".encode()).hexdigest()
        entities.append(CodeEntity(
            id=eid, name=page_name, entity_type=EntityType.CLASS,
            file_path=str(file_path), start_line=1, end_line=len(lines),
            signature=f"page {page_name}" + (f" — {title}" if title else ""),
            body=self._extract_text_content(content)[:2000],
            docstring=title,
        ))

        # Find <script> elements and parse their JS content
        self._find_scripts(tree.root_node, source, file_path, entities)

        return entities

    def _find_scripts(self, node, source, file_path, entities):
        if node.type == "script_element":
            raw_text = node.child_by_field_name("raw_text")
            if raw_text:
                js_source = raw_text.text
                js_tree = self._js_parser.parse(js_source)
                offset = raw_text.start_point[0]
                self._extract_js_functions(js_tree.root_node, js_source, file_path, entities, offset)
            return

        for child in node.children:
            self._find_scripts(child, source, file_path, entities)

    def _extract_js_functions(self, node, source, file_path, entities, line_offset):
        if node.type in ("function_declaration", "generator_function_declaration"):
            name = None
            for child in node.children:
                if child.type == "identifier":
                    name = child.text.decode("utf-8")
                    break
            if name:
                start_line = node.start_point[0] + line_offset + 1
                end_line = node.end_point[0] + line_offset + 1
                body = source[node.start_byte:node.end_byte].decode("utf-8", errors="ignore")[:2000]
                params = self._get_params(node, source)
                eid = hashlib.md5(f"{file_path}:{name}:{start_line}".encode()).hexdigest()
                entities.append(CodeEntity(
                    id=eid, name=name, entity_type=EntityType.FUNCTION,
                    file_path=str(file_path), start_line=start_line, end_line=end_line,
                    signature=f"function {name}({params})", body=body,
                    calls=self._extract_calls(node, source),
                ))
            return

        for child in node.children:
            self._extract_js_functions(child, source, file_path, entities, line_offset)

    def _get_params(self, node, source):
        for child in node.children:
            if child.type == "formal_parameters":
                return child.text.decode("utf-8").strip("()")
        return ""

    def _extract_calls(self, node, source):
        calls = set()
        self._find_calls(node, source, calls)
        return list(calls)

    def _find_calls(self, node, source, calls):
        if node.type == "call_expression":
            func = node.child_by_field_name("function")
            if func and func.type == "identifier":
                calls.add(func.text.decode("utf-8"))
        for child in node.children:
            self._find_calls(child, source, calls)

    def _extract_title(self, node, source):
        if node.type == "element":
            tag = None
            for child in node.children:
                if child.type == "start_tag":
                    for c in child.children:
                        if c.type == "tag_name" and c.text.decode("utf-8") == "title":
                            tag = "title"
                if tag == "title" and child.type == "text":
                    return child.text.decode("utf-8").strip()
        for child in node.children:
            result = self._extract_title(child, source)
            if result:
                return result
        return None

    def _extract_text_content(self, content: str) -> str:
        text = re.sub(r"<script[^>]*>.*?</script>", " ", content, flags=re.DOTALL)
        text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text
