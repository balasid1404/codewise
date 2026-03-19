import hashlib
import re
from pathlib import Path
import javalang
from .entities import CodeEntity, EntityType

# Pattern for UPPER_SNAKE_CASE constants referenced in code
_CONST_REF_PATTERN = re.compile(r'\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b')
# Pattern for qualified field access: ClassName.CONSTANT_NAME
_QUALIFIED_REF_PATTERN = re.compile(r'\b([A-Z][a-zA-Z0-9]*\.[A-Z][A-Z0-9_]+)\b')


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

        # Extract file-level imports
        file_imports = [imp.path for imp in tree.imports] if tree.imports else []

        for _, class_node in tree.filter(javalang.tree.ClassDeclaration):
            ent = self._extract_class(class_node, file_path, lines, package)
            ent.imports = file_imports
            entities.append(ent)

            for method in class_node.methods:
                ent = self._extract_method(method, file_path, lines, class_node.name, package)
                ent.imports = file_imports
                entities.append(ent)

            # Extract static/final fields (constants)
            for field_decl in class_node.fields:
                for ent in self._extract_fields(field_decl, file_path, lines, class_name=class_node.name, package=package):
                    ent.imports = file_imports
                    entities.append(ent)

            # Gap 3: Extract static initializer blocks as pseudo-method entities
            static_init_idx = 0
            for member in (class_node.body or []):
                if isinstance(member, javalang.tree.BlockStatement) or (
                    hasattr(member, 'modifiers') and member.modifiers and 'static' in member.modifiers
                    and not hasattr(member, 'name')
                ):
                    static_init_idx += 1
                    ent = self._extract_static_initializer(
                        member, class_node, file_path, lines, package, static_init_idx
                    )
                    if ent:
                        ent.imports = file_imports
                        entities.append(ent)

            # Fallback: regex-based static initializer extraction for cases javalang misses
            regex_inits = self._extract_static_initializers_regex(
                class_node, file_path, lines, package, existing_count=static_init_idx
            )
            for ent in regex_inits:
                ent.imports = file_imports
                entities.append(ent)

        # Extract enum declarations and their constants
        for _, enum_node in tree.filter(javalang.tree.EnumDeclaration):
            ent = self._extract_enum(enum_node, file_path, lines, package)
            ent.imports = file_imports
            entities.append(ent)

            for const in (enum_node.body.constants or []):
                ent = self._extract_enum_constant(const, enum_node, file_path, lines, package)
                ent.imports = file_imports
                entities.append(ent)

            for method in (enum_node.body.methods or []) if hasattr(enum_node.body, 'methods') and enum_node.body.methods else []:
                ent = self._extract_method(method, file_path, lines, enum_node.name, package)
                ent.imports = file_imports
                entities.append(ent)

        # Gap 5: Extract constant/field references for all entities with bodies
        for ent in entities:
            if ent.body:
                ent.references = self._extract_references(ent.body, ent.name)

        return entities

    def _extract_class(self, node, file_path: Path, lines: list[str], package: str | None) -> CodeEntity:
        start_line = node.position.line if node.position else 1
        end_line = self._find_end_line(lines, start_line)
        body = "\n".join(lines[start_line - 1:end_line])
        annotations = [f"@{a.name}" for a in node.annotations] if node.annotations else []
        extends = f" extends {node.extends.name}" if node.extends else ""
        implements = ""
        base_classes = []
        if node.extends:
            base_classes.append(node.extends.name)
        if node.implements:
            impl_names = [i.name for i in node.implements]
            implements = f" implements {', '.join(impl_names)}"
            base_classes.extend(impl_names)
        sig = f"class {node.name}{extends}{implements}"

        entity_id = hashlib.md5(f"{file_path}:{node.name}:{start_line}".encode()).hexdigest()

        return CodeEntity(
            id=entity_id,
            name=node.name,
            entity_type=EntityType.CLASS,
            file_path=str(file_path),
            start_line=start_line,
            end_line=end_line,
            signature=sig,
            body=body,
            package=package,
            docstring=node.documentation,
            annotations=annotations,
            base_classes=base_classes
        )

    def _extract_method(self, node, file_path: Path, lines: list[str], class_name: str, package: str | None) -> CodeEntity:
        start_line = node.position.line if node.position else 1
        end_line = self._find_end_line(lines, start_line)
        body = "\n".join(lines[start_line - 1:end_line])
        signature = self._get_signature(node)
        calls = self._extract_calls(node)
        annotations = [f"@{a.name}" for a in node.annotations] if node.annotations else []

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
            calls=calls,
            annotations=annotations
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

    def _extract_enum(self, node, file_path: Path, lines: list[str], package: str | None) -> CodeEntity:
        """Extract an enum declaration as a CLASS-like entity."""
        start_line = node.position.line if node.position else 1
        end_line = self._find_end_line(lines, start_line)
        body = "\n".join(lines[start_line - 1:end_line])
        annotations = [f"@{a.name}" for a in node.annotations] if node.annotations else []

        implements = ""
        base_classes = []
        if node.implements:
            impl_names = [i.name for i in node.implements]
            implements = f" implements {', '.join(impl_names)}"
            base_classes.extend(impl_names)

        sig = f"enum {node.name}{implements}"
        entity_id = hashlib.md5(f"{file_path}:{node.name}:{start_line}".encode()).hexdigest()

        return CodeEntity(
            id=entity_id,
            name=node.name,
            entity_type=EntityType.ENUM,
            file_path=str(file_path),
            start_line=start_line,
            end_line=end_line,
            signature=sig,
            body=body,
            package=package,
            docstring=node.documentation,
            annotations=annotations,
            base_classes=base_classes
        )

    def _extract_enum_constant(self, const, enum_node, file_path: Path, lines: list[str], package: str | None) -> CodeEntity:
        """Extract individual enum constants (e.g. HAWKFIRE_ALL_DEVICES_ANNUAL_DISCOUNTED)."""
        start_line = const.position.line if const.position else enum_node.position.line
        # Enum constants are typically single-line or a few lines with arguments
        end_line = start_line
        # Try to find the end of this constant (next comma or semicolon)
        for i in range(start_line - 1, min(start_line + 20, len(lines))):
            line = lines[i].strip()
            if line.endswith(',') or line.endswith(';') or line.endswith(')') or line.endswith('),'):
                end_line = i + 1
                break
        if end_line < start_line:
            end_line = start_line

        body = "\n".join(lines[start_line - 1:end_line])

        # Build signature with arguments if present
        args = ""
        if const.arguments:
            arg_strs = []
            for arg in const.arguments:
                arg_strs.append(str(arg) if not hasattr(arg, 'value') else str(arg.value))
            args = f"({', '.join(arg_strs)})"
        sig = f"{enum_node.name}.{const.name}{args}"

        entity_id = hashlib.md5(f"{file_path}:{enum_node.name}.{const.name}:{start_line}".encode()).hexdigest()

        return CodeEntity(
            id=entity_id,
            name=const.name,
            entity_type=EntityType.FIELD,
            file_path=str(file_path),
            start_line=start_line,
            end_line=end_line,
            signature=sig,
            body=body,
            class_name=enum_node.name,
            package=package,
        )

    def _extract_fields(self, field_decl, file_path: Path, lines: list[str], class_name: str, package: str | None) -> list[CodeEntity]:
        """Extract static/final field declarations (constants like PLAN_ID)."""
        entities = []
        modifiers = set(field_decl.modifiers) if field_decl.modifiers else set()

        # Only extract static or final fields — these are the constants we care about
        if not (modifiers & {'static', 'final'}):
            return entities

        field_type = field_decl.type.name if field_decl.type else "Object"

        for declarator in field_decl.declarators:
            start_line = declarator.position.line if declarator.position else (field_decl.position.line if field_decl.position else 1)
            # Find end of statement (semicolon)
            end_line = start_line
            for i in range(start_line - 1, min(start_line + 10, len(lines))):
                if ';' in lines[i]:
                    end_line = i + 1
                    break

            body = "\n".join(lines[start_line - 1:end_line])
            mod_str = " ".join(sorted(modifiers))
            sig = f"{mod_str} {field_type} {declarator.name}"

            entity_id = hashlib.md5(f"{file_path}:{class_name}.{declarator.name}:{start_line}".encode()).hexdigest()

            entities.append(CodeEntity(
                id=entity_id,
                name=declarator.name,
                entity_type=EntityType.FIELD,
                file_path=str(file_path),
                start_line=start_line,
                end_line=end_line,
                signature=sig,
                body=body,
                class_name=class_name,
                package=package,
            ))

        return entities

    # ── Gap 3: Static initializer block extraction ───────────────

    def _extract_static_initializer(self, member, class_node, file_path: Path,
                                     lines: list[str], package: str | None,
                                     idx: int) -> CodeEntity | None:
        """Extract a static initializer block as a pseudo-method entity."""
        if not hasattr(member, 'position') or not member.position:
            return None

        start_line = member.position.line
        end_line = self._find_end_line(lines, start_line)
        body = "\n".join(lines[start_line - 1:end_line])

        name = f"<static_init_{idx}>"
        sig = f"static {{ /* initializer block {idx} */ }}"
        entity_id = hashlib.md5(
            f"{file_path}:{class_node.name}.{name}:{start_line}".encode()
        ).hexdigest()

        return CodeEntity(
            id=entity_id,
            name=name,
            entity_type=EntityType.METHOD,
            file_path=str(file_path),
            start_line=start_line,
            end_line=end_line,
            signature=sig,
            body=body,
            class_name=class_node.name,
            package=package,
        )

    def _extract_static_initializers_regex(self, class_node, file_path: Path,
                                            lines: list[str], package: str | None,
                                            existing_count: int) -> list[CodeEntity]:
        """Regex fallback: find 'static {' blocks that javalang may not expose as AST nodes."""
        entities = []
        class_start = class_node.position.line if class_node.position else 1
        class_end = self._find_end_line(lines, class_start)
        idx = existing_count

        i = class_start - 1
        while i < class_end:
            line = lines[i].strip()
            # Match standalone "static {" that isn't a method or field
            if re.match(r'^static\s*\{', line):
                idx += 1
                start_line = i + 1
                end_line = self._find_end_line(lines, start_line)
                body = "\n".join(lines[start_line - 1:end_line])

                name = f"<static_init_{idx}>"
                sig = f"static {{ /* initializer block {idx} */ }}"
                entity_id = hashlib.md5(
                    f"{file_path}:{class_node.name}.{name}:{start_line}".encode()
                ).hexdigest()

                entities.append(CodeEntity(
                    id=entity_id,
                    name=name,
                    entity_type=EntityType.METHOD,
                    file_path=str(file_path),
                    start_line=start_line,
                    end_line=end_line,
                    signature=sig,
                    body=body,
                    class_name=class_node.name,
                    package=package,
                ))
                i = end_line  # skip past this block
            else:
                i += 1

        return entities

    # ── Gap 5: Field/constant reference extraction ───────────────

    def _extract_references(self, body: str, own_name: str) -> list[str]:
        """Extract UPPER_SNAKE_CASE constants and qualified field references from body text.

        Returns deduplicated list of referenced constant/field names.
        Excludes the entity's own name to avoid self-references.
        """
        refs = set()

        # UPPER_SNAKE_CASE constants: HAWKFIRE_ALL_DEVICES_ANNUAL, PLAN_NAME_TO_TYPE_MAP
        for m in _CONST_REF_PATTERN.finditer(body):
            name = m.group(1)
            if name != own_name and len(name) > 3:
                refs.add(name)

        # Qualified references: PlanName.HAWKFIRE_ALL_DEVICES_ANNUAL_NON_DISCOUNTED
        for m in _QUALIFIED_REF_PATTERN.finditer(body):
            full_ref = m.group(1)
            parts = full_ref.split(".")
            if len(parts) == 2:
                refs.add(parts[1])  # Add the constant name
                refs.add(full_ref)  # Add the qualified form too

        return sorted(refs)
