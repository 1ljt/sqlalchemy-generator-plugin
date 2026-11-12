"""Assemble render contexts and produce output modules via Jinja2.

The import header is built in Python (deterministic, deduplicated, PEP 8
ordered) and handed to the template as one string; the class bodies live in the
Jinja2 templates so users can restyle them without touching Python.
"""

import os
import re
from dataclasses import dataclass, field

from jinja2 import Environment, FileSystemLoader, StrictUndefined

DEFAULT_TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")

MODEL_TEMPLATE = "model.py.j2"
BASE_TEMPLATE = "base.py.j2"
INIT_TEMPLATE = "init.py.j2"


@dataclass
class ColumnView:
    """Template-facing view of one column."""

    attr_name: str
    db_name: str
    annotation: str
    args: str
    python_type: str
    sa_type: str
    nullable: bool
    primary_key: bool
    autoincrement: bool
    unique: bool
    index: bool
    comment: str = None
    notes: tuple = ()
    unmapped: bool = False


@dataclass
class RelationshipView:
    attr_name: str
    annotation: str
    args: str
    target_class: str
    is_collection: bool
    back_populates: str = ""


@dataclass
class TableView:
    class_name: str
    table_name: str
    schema: str = None
    docstring: str = None
    columns: list = field(default_factory=list)
    relationships: list = field(default_factory=list)
    table_args: list = field(default_factory=list)

    @property
    def is_empty(self):
        return not self.columns and not self.relationships


@dataclass
class ModuleView:
    """Everything one output module's template needs."""

    file_name: str
    kind: str
    header: str
    base_class_name: str
    tables: list = field(default_factory=list)
    table_names: list = field(default_factory=list)
    exports: list = field(default_factory=list)


def _make_environment(template_dir):
    return Environment(
        loader=FileSystemLoader(template_dir, encoding="utf-8"),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        autoescape=False,
    )


def _safe_docstring(text):
    if not text:
        return None
    cleaned = " ".join(str(text).split()).strip()
    if not cleaned:
        return None
    return cleaned.replace('"""', "'\"'\"'")


def build_column_view(col, emit_comments):
    return ColumnView(
        attr_name=col.attr_name,
        db_name=col.name,
        annotation=col.annotation,
        args=", ".join(col.mapped_column_args(emit_comments=emit_comments)),
        python_type=col.python_type,
        sa_type=col.sa_type_expr,
        nullable=col.nullable,
        primary_key=col.primary_key,
        autoincrement=col.autoincrement,
        unique=col.unique,
        index=col.index,
        comment=col.comment,
        notes=col.type_notes,
        unmapped=col.unmapped,
    )


def build_table_view(table, emit_comments):
    relationships = []
    for rel in table.relationships:
        annotation, kwargs = rel.render()
        relationships.append(
            RelationshipView(
                attr_name=rel.attr_name,
                annotation=annotation,
                args=kwargs,
                target_class=rel.target_class,
                is_collection=rel.is_collection,
                back_populates=rel.back_populates,
            )
        )
    return TableView(
        class_name=table.class_name,
        table_name=table.name,
        schema=table.schema,
        docstring=_safe_docstring(table.comment),
        columns=[build_column_view(c, emit_comments) for c in table.columns],
        relationships=relationships,
        table_args=list(table.table_args),
    )


def _collect_imports(tables):
    sa_names, dialect_lines, py_lines = set(), set(), set()
    needs_text = needs_fk = needs_relationship = False
    for table in tables:
        sa_names.update(table.extra_sa_imports)
        for col in table.columns:
            sa_names.update(col.sa_imports)
            for line in col.py_imports:
                (dialect_lines if line.startswith("from sqlalchemy.dialects.") else py_lines).add(line)
            if col.server_default:
                needs_text = True
            if col.foreign_key:
                needs_fk = True
        if table.relationships:
            needs_relationship = True
    if needs_fk:
        sa_names.add("ForeignKey")
    if needs_text:
        sa_names.add("text")
    return sorted(sa_names), sorted(dialect_lines), sorted(py_lines), needs_relationship


_FROM_IMPORT = re.compile(r"^from\s+(\S+)\s+import\s+(.+)$")


def merge_from_imports(lines):
    """Collapse repeated `from X import a` / `from X import b` into one line.

    Plain `import x` lines are preserved and sorted ahead of `from` imports.
    """
    plain, merged, order = [], {}, []
    for line in lines:
        match = _FROM_IMPORT.match(line.strip())
        if not match:
            plain.append(line.strip())
            continue
        module, names = match.group(1), [n.strip() for n in match.group(2).split(",") if n.strip()]
        if module not in merged:
            merged[module] = []
            order.append(module)
        merged[module].extend(names)
    out = sorted(set(plain))
    out += [f"from {m} import {', '.join(sorted(set(merged[m])))}" for m in sorted(order)]
    return out


def build_header(
    tables,
    *,
    base_class_name,
    base_import_line,
    use_future_annotations=True,
    type_checking_imports=(),
):
    """Render the module import block. Blank-line separated, PEP 8 grouped."""
    sa_names, dialect_lines, py_lines, needs_relationship = _collect_imports(tables)

    future = ["from __future__ import annotations"] if use_future_annotations else []

    stdlib = list(py_lines)
    if type_checking_imports:
        stdlib.append("from typing import TYPE_CHECKING")
    stdlib = merge_from_imports(stdlib)

    third_party = []
    if sa_names:
        third_party.append(f"from sqlalchemy import {', '.join(sa_names)}")
    third_party.extend(dialect_lines)
    if tables:
        orm_names = ["Mapped", "mapped_column"]
        if not base_import_line:
            orm_names.append("DeclarativeBase")
        if needs_relationship:
            orm_names.append("relationship")
        third_party.append(f"from sqlalchemy.orm import {', '.join(sorted(set(orm_names)))}")
    third_party = merge_from_imports(third_party)

    local = [base_import_line] if base_import_line else []

    paragraphs = ["\n".join(g) for g in (future, stdlib, third_party, local) if g]

    if type_checking_imports:
        block = ["if TYPE_CHECKING:"]
        for module, class_names in type_checking_imports:
            block.append(f"    from {module} import {', '.join(sorted(class_names))}")
        paragraphs.append("\n".join(block))

    if not paragraphs:
        return ""
    # Two trailing newlines: the template adds one more, giving the PEP 8 two
    # blank lines before the first top-level class.
    return "\n\n".join(paragraphs) + "\n\n"


def _type_checking_imports(tables, local_class_names):
    """Relative imports for relationship targets defined in sibling modules."""
    needed = {}
    for table in tables:
        for rel in table.relationships:
            if rel.target_class in local_class_names or not rel.target_module:
                continue
            needed.setdefault(rel.target_module, set()).add(rel.target_class)
    return sorted((f".{module}", names) for module, names in needed.items())


class Renderer:
    """Turns TableMeta lists into GeneratedFile lists."""

    def __init__(self, generation, template_dir=None):
        self.generation = generation
        self.template_dir = template_dir or DEFAULT_TEMPLATE_DIR
        self.env = _make_environment(self.template_dir)
        self.base_class_name = generation.base_class_name
        self.base_import_line = generation.base_import or (
            f"from .{os.path.splitext(generation.base_file_name)[0]} import {self.base_class_name}"
            if generation.generate_base_file
            else ""
        )

    # --- helpers ---------------------------------------------------------
    def _annotate_modules(self, tables):
        """Give every relationship its target's module name for TYPE_CHECKING."""
        module_by_class = {t.class_name: t.module_name for t in tables}
        for table in tables:
            for rel in table.relationships:
                rel.target_module = module_by_class.get(rel.target_class, "")

    def _render_base(self):
        template = self.env.get_template(BASE_TEMPLATE)
        content = template.render(
            base_class_name=self.base_class_name,
            use_future_annotations=self.generation.use_future_annotations,
        )
        return {"relative_path": self.generation.base_file_name, "content": content,
                "kind": "base", "table_names": []}

    def _render_init(self, base_line, model_imports, exports):
        template = self.env.get_template(INIT_TEMPLATE)
        content = template.render(
            base_import_line=base_line,
            model_imports=model_imports,
            exports=list(exports),
        )
        return {"relative_path": self.generation.init_file_name, "content": content,
                "kind": "init", "table_names": []}

    # --- public API ------------------------------------------------------
    def render(self, tables):
        """Produce the full list of output file dicts."""
        self._annotate_modules(tables)
        files = []

        base_is_own_file = self.generation.generate_base_file and not self.generation.base_import
        if base_is_own_file:
            files.append(self._render_base())

        base_line = None
        if base_is_own_file:
            base_module = os.path.splitext(self.generation.base_file_name)[0]
            base_line = f"from .{base_module} import {self.base_class_name}"
        elif self.generation.base_import:
            base_line = self.generation.base_import

        class_names = [t.class_name for t in tables]
        if self.generation.mode == "single":
            files.append(self._render_single(tables))
            stem = os.path.splitext(self.generation.single_file_name)[0]
            model_imports = [{"module": stem, "names": class_names}] if class_names else []
        else:
            files.extend(self._render_multiple(tables))
            model_imports = [
                {"module": t.module_name, "names": [t.class_name]} for t in tables
            ]

        if self.generation.generate_init:
            exports = ([self.base_class_name] if base_line else []) + class_names
            files.append(self._render_init(base_line, model_imports, exports))

        return files

    def _render_single(self, tables):
        template = self.env.get_template(MODEL_TEMPLATE)
        views = [build_table_view(t, self.generation.include_column_comments) for t in tables]
        local_classes = {t.class_name for t in tables}
        header = build_header(
            tables,
            base_class_name=self.base_class_name,
            base_import_line=self.base_import_line,
            use_future_annotations=self.generation.use_future_annotations,
            # Single-file output defines every class locally: no forward imports.
            type_checking_imports=_type_checking_imports(tables, local_classes),
        )
        content = template.render(
            header=header,
            base_class_name=self.base_class_name,
            tables=views,
            emit_comments=self.generation.include_column_comments,
        )
        return {
            "relative_path": self.generation.single_file_name,
            "content": content,
            "kind": "model",
            "table_names": [t.name for t in tables],
        }

    def _render_multiple(self, tables):
        template = self.env.get_template(MODEL_TEMPLATE)
        files = []
        for table in tables:
            local_classes = {table.class_name}
            header = build_header(
                [table],
                base_class_name=self.base_class_name,
                base_import_line=self.base_import_line,
                use_future_annotations=self.generation.use_future_annotations,
                type_checking_imports=_type_checking_imports([table], local_classes),
            )
            content = template.render(
                header=header,
                base_class_name=self.base_class_name,
                tables=[build_table_view(table, self.generation.include_column_comments)],
                emit_comments=self.generation.include_column_comments,
            )
            files.append({
                "relative_path": f"{table.module_name}.py",
                "content": content,
                "kind": "model",
                "table_names": [table.name],
            })
        return files
