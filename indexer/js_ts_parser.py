"""JavaScript/TypeScript parser using tree-sitter AST."""

import hashlib
from pathlib import Path
import tree_sitter_javascript as tsjs
import tree_sitter_typescript as tsts
from tree_sitter import Language, Parser
from .entities import CodeEntity, EntityType


class JsTsParser:
    """Parse JS/TS files using tree-sitter for accurate AST extraction."""

    def __init__(self):
        self._js_parser = Parser(Language(tsjs.language()))
        self._ts_parser = Parser(Language(tsts.language_typescript()))

    def parse_file(self, file_path: Path) -> list[CodeEntity]:
        try:
            content = file_path.read_text(errors="ignore")
            source = content.encode("utf-8")
        except Exception:
            return []

        parser = self._ts_parser if file_path.suffix == ".ts" else self._js_parser
        tree = parser.parse(source)

        # Extract file-level imports
        file_imports = self._extract_imports(tree.root_node, source)

        entities = []
        self._walk(tree.root_node, file_path, source, entities, class_name=None)

        # Attach imports to all entities from this file
        for ent in entities:
            ent.imports = file_imports

        return entities

    def _extract_imports(self, root_node, source):
        """Extract import paths from import statements."""
        imports = []
        for child in root_node.children:
            if child.type == "import_statement":
                src_node = child.child_by_field_name("source")
                if src_node:
                    path = src_node.text.decode("utf-8").strip("'\"")
                    imports.append(path)
            elif child.type == "export_statement":
                src_node = child.child_by_field_name("source")
                if src_node:
                    path = src_node.text.decode("utf-8").strip("'\"")
                    imports.append(path)
        return imports

    def _walk(self, node, file_path, source, entities, class_name):
        """Recursively walk the AST and extract entities."""
        ntype = node.type

        if ntype == "class_declaration":
            name = self._child_text(node, "type_identifier", source) or self._child_text(node, "identifier", source)
            if name:
                # Extract extends clause
                base_classes = []
                heritage = self._child_by_type(node, "class_heritage")
                if heritage:
                    for child in heritage.children:
                        if child.type in ("identifier", "type_identifier"):
                            base_classes.append(child.text.decode("utf-8"))
                        elif child.type == "extends_clause":
                            for c in child.children:
                                if c.type in ("identifier", "type_identifier"):
                                    base_classes.append(c.text.decode("utf-8"))

                extends_str = f" extends {', '.join(base_classes)}" if base_classes else ""
                ent = self._make_entity(node, file_path, source, name, EntityType.CLASS, f"class {name}{extends_str}")
                ent.base_classes = base_classes
                entities.append(ent)
            # Walk class body for methods
            body = self._child_by_type(node, "class_body")
            if body:
                for child in body.children:
                    self._walk(child, file_path, source, entities, class_name=name)
            return

        if ntype in ("function_declaration", "generator_function_declaration"):
            name = self._child_text(node, "identifier", source)
            if name:
                params = self._get_params(node, source)
                sig = f"function {name}({params})"
                ent = self._make_entity(node, file_path, source, name, EntityType.FUNCTION, sig, class_name)
                ent.calls = self._extract_calls(node, source)
                entities.append(ent)
            return

        if ntype == "method_definition":
            name = self._child_text(node, "property_identifier", source)
            if name:
                params = self._get_params(node, source)
                sig = f"{name}({params})"
                ent = self._make_entity(node, file_path, source, name, EntityType.METHOD, sig, class_name)
                ent.calls = self._extract_calls(node, source)
                entities.append(ent)
            return

        if ntype in ("lexical_declaration", "variable_declaration"):
            # Check for arrow functions: const foo = (...) => { ... }
            for decl in node.children:
                if decl.type == "variable_declarator":
                    name_node = decl.child_by_field_name("name")
                    value_node = decl.child_by_field_name("value")
                    if name_node and value_node and value_node.type == "arrow_function":
                        name = name_node.text.decode("utf-8")
                        params = self._get_params(value_node, source)
                        sig = f"const {name} = ({params}) =>"
                        ent = self._make_entity(value_node, file_path, source, name, EntityType.FUNCTION, sig, class_name)
                        ent.calls = self._extract_calls(value_node, source)
                        entities.append(ent)
            return

        if ntype == "export_statement":
            for child in node.children:
                self._walk(child, file_path, source, entities, class_name)
            return

        # Recurse into other nodes
        for child in node.children:
            self._walk(child, file_path, source, entities, class_name)

    def _make_entity(self, node, file_path, source, name, etype, signature, class_name=None):
        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1
        body = source[node.start_byte:node.end_byte].decode("utf-8", errors="ignore")[:2000]
        eid = hashlib.md5(f"{file_path}:{name}:{start_line}".encode()).hexdigest()
        return CodeEntity(
            id=eid, name=name, entity_type=etype,
            file_path=str(file_path), start_line=start_line, end_line=end_line,
            signature=signature, body=body, class_name=class_name,
        )

    def _child_text(self, node, child_type, source):
        for child in node.children:
            if child.type == child_type:
                return child.text.decode("utf-8")
        return None

    def _child_by_type(self, node, child_type):
        for child in node.children:
            if child.type == child_type:
                return child
        return None

    def _get_params(self, node, source):
        params_node = self._child_by_type(node, "formal_parameters")
        if params_node:
            return params_node.text.decode("utf-8").strip("()")
        return ""

    def _extract_calls(self, node, source):
        calls = set()
        self._find_calls(node, source, calls)
        return list(calls)

    def _find_calls(self, node, source, calls):
        if node.type == "call_expression":
            func = node.child_by_field_name("function")
            if func:
                if func.type == "identifier":
                    calls.add(func.text.decode("utf-8"))
                elif func.type == "member_expression":
                    prop = func.child_by_field_name("property")
                    if prop:
                        calls.add(prop.text.decode("utf-8"))
        for child in node.children:
            self._find_calls(child, source, calls)
