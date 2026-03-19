"""HTML parser using tree-sitter — extracts page entities and inline scripts."""

import re
import hashlib
from pathlib import Path
import tree_sitter_javascript as tsjs
import tree_sitter_html as tshtml
from tree_sitter import Language, Parser
from .entities import CodeEntity, EntityType

# Pattern for UPPER_SNAKE_CASE constants referenced in code
_CONST_REF_PATTERN = re.compile(r'\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b')
# Pattern for qualified access: ClassName.CONSTANT_NAME
_QUALIFIED_REF_PATTERN = re.compile(r'\b([A-Z][a-zA-Z0-9]*\.[A-Z][A-Z0-9_]+)\b')


class HtmlParser:
    """Parse HTML files to extract page components and inline JS functions."""

    def __init__(self):
        self._html_parser = Parser(Language(tshtml.language()))
        self._js_parser = Parser(Language(tsjs.language()))

    def parse_file(self, file_path: Path) -> list[CodeEntity]:
        try:
            content = file_path.read_text(errors="ignore")
            source = content.encode("utf-8")
        except Exception:
            return []

        tree = self._html_parser.parse(source)
        entities = []
        lines = content.splitlines()

        # Extract external references (script src, link href, etc.)
        external_refs = self._extract_external_refs(tree.root_node, source)

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
            imports=external_refs,  # script src and link href as "imports"
            file_imports=external_refs,  # same for file-level graph
        ))

        # Find <script> elements and parse their JS content
        self._find_scripts(tree.root_node, source, file_path, entities, external_refs)

        # Extract constant/field references for all entities with bodies
        for ent in entities:
            if ent.body:
                ent.references = self._extract_references(ent.body, ent.name)

        return entities

    def _find_scripts(self, node, source, file_path, entities, external_refs=None):
        if node.type == "script_element":
            # Check for src attribute (external script)
            start_tag = next((c for c in node.children if c.type == "start_tag"), None)
            if start_tag:
                for attr in start_tag.children:
                    if attr.type == "attribute":
                        name_node = next((c for c in attr.children if c.type == "attribute_name"), None)
                        val_node = next((c for c in attr.children if c.type in ("attribute_value", "quoted_attribute_value")), None)
                        if name_node and val_node and name_node.text.decode("utf-8") == "src":
                            # External script — already captured in external_refs
                            pass

            raw_text = next((c for c in node.children if c.type == "raw_text"), None)
            if raw_text:
                js_source = raw_text.text
                js_tree = self._js_parser.parse(js_source)
                offset = raw_text.start_point[0]
                self._extract_js_functions(js_tree.root_node, js_source, file_path, entities, offset, external_refs)
            return

        for child in node.children:
            self._find_scripts(child, source, file_path, entities, external_refs)

    def _extract_js_functions(self, node, source, file_path, entities, line_offset, external_refs=None):
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
                    imports=external_refs or [],
                ))
            return

        for child in node.children:
            self._extract_js_functions(child, source, file_path, entities, line_offset, external_refs)

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

    def _extract_external_refs(self, node, source) -> list[str]:
        """Extract script src and link href references from HTML."""
        refs = []
        self._walk_for_refs(node, source, refs)
        return refs

    def _walk_for_refs(self, node, source, refs):
        if node.type == "element":
            start_tag = next((c for c in node.children if c.type == "start_tag"), None)
            if start_tag:
                tag_name = None
                attrs = {}
                for child in start_tag.children:
                    if child.type == "tag_name":
                        tag_name = child.text.decode("utf-8").lower()
                    elif child.type == "attribute":
                        aname = None
                        aval = None
                        for ac in child.children:
                            if ac.type == "attribute_name":
                                aname = ac.text.decode("utf-8").lower()
                            elif ac.type in ("attribute_value", "quoted_attribute_value"):
                                aval = ac.text.decode("utf-8").strip("'\"")
                        if aname and aval:
                            attrs[aname] = aval

                # script src
                if tag_name == "script" and "src" in attrs:
                    src = attrs["src"]
                    if not src.startswith("http") and not src.startswith("//"):
                        refs.append(src)
                # link href (stylesheets, but also JS modules)
                elif tag_name == "link" and "href" in attrs:
                    href = attrs["href"]
                    if not href.startswith("http") and not href.startswith("//"):
                        refs.append(href)

        for child in node.children:
            self._walk_for_refs(child, source, refs)

    def _extract_references(self, body: str, own_name: str) -> list[str]:
        """Extract UPPER_SNAKE_CASE constants and qualified references from body text."""
        refs = set()

        for m in _CONST_REF_PATTERN.finditer(body):
            name = m.group(1)
            if name != own_name and len(name) > 3:
                refs.add(name)

        for m in _QUALIFIED_REF_PATTERN.finditer(body):
            full_ref = m.group(1)
            parts = full_ref.split(".")
            if len(parts) == 2:
                refs.add(parts[1])
                refs.add(full_ref)

        return sorted(refs)
