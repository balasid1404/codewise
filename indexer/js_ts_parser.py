"""JavaScript/TypeScript parser using tree-sitter AST."""

import re
import hashlib
from pathlib import Path
import tree_sitter_javascript as tsjs
import tree_sitter_typescript as tsts
from tree_sitter import Language, Parser
from .entities import CodeEntity, EntityType

# Pattern for UPPER_SNAKE_CASE constants referenced in code
_CONST_REF_PATTERN = re.compile(r'\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b')
# Pattern for qualified access: ClassName.CONSTANT_NAME
_QUALIFIED_REF_PATTERN = re.compile(r'\b([A-Z][a-zA-Z0-9]*\.[A-Z][A-Z0-9_]+)\b')


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

        # Extract constant/field references for all entities with bodies
        for ent in entities:
            if ent.body:
                ent.references = self._extract_references(ent.body, ent.name)

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
            for decl in node.children:
                if decl.type == "variable_declarator":
                    name_node = decl.child_by_field_name("name")
                    value_node = decl.child_by_field_name("value")
                    if name_node and value_node and value_node.type == "arrow_function":
                        # Arrow function: const foo = (...) => { ... }
                        name = name_node.text.decode("utf-8")
                        params = self._get_params(value_node, source)
                        sig = f"const {name} = ({params}) =>"
                        ent = self._make_entity(value_node, file_path, source, name, EntityType.FUNCTION, sig, class_name)
                        ent.calls = self._extract_calls(value_node, source)
                        entities.append(ent)
                    elif name_node and value_node:
                        # Non-function constant: const API_URL = "...", const CONFIG = {...}
                        name = name_node.text.decode("utf-8")
                        # Only extract UPPER_CASE or exported constants (skip loop vars like `i`)
                        if name.isupper() or name[0].isupper() or ntype == "lexical_declaration":
                            # Determine keyword (const/let/var)
                            keyword = "const"
                            for child in node.children:
                                if child.type in ("const", "let", "var"):
                                    keyword = child.type
                                    break
                            sig = f"{keyword} {name}"
                            ent = self._make_entity(decl, file_path, source, name, EntityType.FIELD, sig, class_name)
                            entities.append(ent)
            return

        # TypeScript enum declarations
        if ntype == "enum_declaration":
            name = self._child_text(node, "identifier", source)
            if name:
                sig = f"enum {name}"
                ent = self._make_entity(node, file_path, source, name, EntityType.ENUM, sig, class_name)
                entities.append(ent)
                # Extract enum members
                body_node = self._child_by_type(node, "enum_body")
                if body_node:
                    for member in body_node.children:
                        if member.type == "enum_member" or member.type == "property_identifier":
                            member_name_node = member.child_by_field_name("name") if member.type == "enum_member" else member
                            if member_name_node:
                                mname = member_name_node.text.decode("utf-8")
                                msig = f"{name}.{mname}"
                                ment = self._make_entity(member, file_path, source, mname, EntityType.FIELD, msig, name)
                                entities.append(ment)
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
