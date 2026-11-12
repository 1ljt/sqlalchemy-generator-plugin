"""Renderer: import-header assembly, module layout, and template plumbing."""

import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlalchemy as sa

from engine.protocol import GenerationConfig
from engine.renderer import Renderer, build_header, build_table_view, merge_from_imports
from engine.schema import build_table_meta
from engine.type_mapper import MapperContext

ENGINE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_table(name, columns, pk=("id",), fks=(), **kwargs):
    cols = [{"name": "id", "type": sa.Integer(), "nullable": False, "autoincrement": True}]
    cols += columns
    return build_table_meta(
        name, cols, primary_key={"constrained_columns": list(pk)},
        foreign_keys=[dict(f) for f in fks], ctx=MapperContext(dialect="mysql"), **kwargs
    )


def users_and_orders():
    users = make_table("users", [
        {"name": "email", "type": sa.String(255), "nullable": True},
        {"name": "balance", "type": sa.Numeric(10, 2), "nullable": False},
        {"name": "created_at", "type": sa.DateTime(), "nullable": False,
         "default": "CURRENT_TIMESTAMP"},
    ])
    orders = make_table(
        "orders",
        [{"name": "user_id", "type": sa.Integer(), "nullable": False}],
        fks=[{"constrained_columns": ["user_id"], "referred_table": "users",
              "referred_columns": ["id"]}],
    )
    from engine.relationships import infer_relationships
    infer_relationships([users, orders])
    return [users, orders]


class TestMergeFromImports(unittest.TestCase):
    def test_merges_same_module(self):
        merged = merge_from_imports(["from typing import Any", "from typing import TYPE_CHECKING"])
        self.assertEqual(merged, ["from typing import Any, TYPE_CHECKING"])

    def test_sorts_names_within_a_module(self):
        merged = merge_from_imports(["from datetime import datetime", "from datetime import date"])
        self.assertEqual(merged, ["from datetime import date, datetime"])

    def test_plain_imports_come_first(self):
        merged = merge_from_imports(["from decimal import Decimal", "import uuid"])
        self.assertEqual(merged, ["import uuid", "from decimal import Decimal"])

    def test_deduplicates_identical_lines(self):
        merged = merge_from_imports(["import uuid", "import uuid"])
        self.assertEqual(merged, ["import uuid"])

    def test_different_modules_stay_separate(self):
        merged = merge_from_imports(["from datetime import date", "from decimal import Decimal"])
        self.assertEqual(len(merged), 2)

    def test_empty_input(self):
        self.assertEqual(merge_from_imports([]), [])


class TestBuildHeader(unittest.TestCase):
    def test_groups_are_blank_line_separated(self):
        tables = users_and_orders()
        header = build_header(tables, base_class_name="Base",
                              base_import_line="from .base import Base")
        paragraphs = [p for p in header.split("\n\n") if p.strip()]
        # __future__ / stdlib / third-party / local
        self.assertGreaterEqual(len(paragraphs), 4)
        self.assertTrue(paragraphs[0].startswith("from __future__ import annotations"))
        self.assertTrue(paragraphs[-1].startswith("from .base import Base"))

    def test_future_import_can_be_disabled(self):
        header = build_header(users_and_orders(), base_class_name="Base",
                              base_import_line="from .base import Base",
                              use_future_annotations=False)
        self.assertNotIn("__future__", header)

    def test_text_imported_only_when_server_default_present(self):
        with_default = make_table("t", [{"name": "a", "type": sa.DateTime(), "nullable": False,
                                         "default": "CURRENT_TIMESTAMP"}])
        without = make_table("t", [{"name": "a", "type": sa.DateTime(), "nullable": False}])

        def sa_line(tables):
            header = build_header(tables, base_class_name="Base", base_import_line="x")
            return next((l for l in header.splitlines() if l.startswith("from sqlalchemy import ")), "")

        self.assertRegex(sa_line([with_default]), r"\btext\b")
        self.assertNotRegex(sa_line([without]), r"\btext\b")

    def test_foreignkey_imported_only_when_needed(self):
        tables = users_and_orders()
        self.assertIn("ForeignKey", build_header(tables, base_class_name="Base",
                                                 base_import_line="x"))
        plain = make_table("t", [{"name": "a", "type": sa.Integer(), "nullable": True}])
        self.assertNotIn("ForeignKey", build_header([plain], base_class_name="Base",
                                                    base_import_line="x"))

    def test_relationship_imported_only_when_relationships_exist(self):
        tables = users_and_orders()
        header = build_header(tables, base_class_name="Base", base_import_line="x")
        orm_line = [l for l in header.splitlines() if l.startswith("from sqlalchemy.orm")][0]
        self.assertIn("relationship", orm_line)

        lonely = make_table("t", [{"name": "a", "type": sa.Integer(), "nullable": True}])
        header = build_header([lonely], base_class_name="Base", base_import_line="x")
        orm_line = [l for l in header.splitlines() if l.startswith("from sqlalchemy.orm")][0]
        self.assertNotIn("relationship", orm_line)

    def test_declarativebase_omitted_when_base_is_imported(self):
        header = build_header(users_and_orders(), base_class_name="Base",
                              base_import_line="from .base import Base")
        self.assertNotIn("DeclarativeBase", header)

    def test_declarativebase_included_when_defining_base_inline(self):
        header = build_header(users_and_orders(), base_class_name="Base", base_import_line="")
        self.assertIn("DeclarativeBase", header)

    def test_table_args_imports_included(self):
        table = make_table(
            "t",
            [{"name": "a", "type": sa.Integer(), "nullable": False},
             {"name": "b", "type": sa.Integer(), "nullable": False}],
            pk=(),
        )
        table.table_args = ["UniqueConstraint('a', 'b', name='uq')"]
        table.extra_sa_imports = ("UniqueConstraint",)
        header = build_header([table], base_class_name="Base", base_import_line="x")
        self.assertIn("UniqueConstraint", header)

    def test_type_checking_block_is_last_and_indented(self):
        tables = users_and_orders()
        header = build_header(tables, base_class_name="Base", base_import_line="from .base import Base",
                              type_checking_imports=[(".orders", {"Order"})])
        self.assertTrue(header.rstrip().endswith("from .orders import Order"))
        self.assertIn("if TYPE_CHECKING:", header)
        self.assertIn("    from .orders import Order", header)
        self.assertIn("from typing import TYPE_CHECKING", header)

    def test_header_ends_with_exactly_two_newlines(self):
        header = build_header(users_and_orders(), base_class_name="Base",
                              base_import_line="from .base import Base")
        self.assertTrue(header.endswith("\n\n"))
        self.assertFalse(header.endswith("\n\n\n"))

    def test_empty_tables_produce_minimal_header(self):
        header = build_header([], base_class_name="Base", base_import_line="")
        self.assertNotIn("sqlalchemy.orm", header)


class TestBuildTableView(unittest.TestCase):
    def test_column_view_fields(self):
        table = make_table("users", [{"name": "userName", "type": sa.String(50), "nullable": False}])
        view = build_table_view(table, emit_comments=False)
        self.assertEqual(view.class_name, "User")
        self.assertEqual(view.table_name, "users")
        column = view.columns[1]
        self.assertEqual(column.attr_name, "user_name")
        self.assertEqual(column.db_name, "userName")
        self.assertEqual(column.annotation, "Mapped[str]")
        self.assertEqual(column.args, "'userName', String(50), nullable=False")

    def test_docstring_is_normalised_and_quote_safe(self):
        table = make_table("t", [], comment='  multi   line\n comment with """ quotes  ')
        view = build_table_view(table, emit_comments=False)
        self.assertNotIn('"""', view.docstring)
        self.assertNotIn("\n", view.docstring)
        self.assertIn("multi line", view.docstring)

    def test_empty_docstring_becomes_none(self):
        self.assertIsNone(build_table_view(make_table("t", [], comment="   "),
                                           emit_comments=False).docstring)

    def test_is_empty_flags_a_table_with_no_members(self):
        # Built directly: make_table() always injects an `id` column.
        table = build_table_meta("t", [], primary_key={"constrained_columns": []},
                                 ctx=MapperContext())
        self.assertTrue(build_table_view(table, emit_comments=False).is_empty)

    def test_relationship_view_annotation(self):
        tables = users_and_orders()
        view = build_table_view(tables[0], emit_comments=False)
        rel = view.relationships[0]
        self.assertTrue(rel.is_collection)
        self.assertEqual(rel.annotation, 'Mapped[list["Order"]]')
        self.assertIn('back_populates="user"', rel.args)


class TestRendererLayout(unittest.TestCase):
    def render(self, mode, **overrides):
        generation = GenerationConfig.from_dict({"mode": mode, "tables": ["users", "orders"], **overrides})
        return Renderer(generation).render(users_and_orders())

    def by_path(self, files):
        return {f["relative_path"]: f for f in files}

    def test_multiple_mode_file_layout(self):
        files = self.by_path(self.render("multiple"))
        self.assertEqual(set(files), {"base.py", "users.py", "orders.py", "__init__.py"})
        self.assertEqual(files["base.py"]["kind"], "base")
        self.assertEqual(files["users.py"]["kind"], "model")
        self.assertEqual(files["__init__.py"]["kind"], "init")

    def test_multiple_mode_records_source_tables(self):
        files = self.by_path(self.render("multiple"))
        self.assertEqual(files["users.py"]["table_names"], ["users"])
        self.assertEqual(files["orders.py"]["table_names"], ["orders"])

    def test_single_mode_file_layout(self):
        files = self.by_path(self.render("single"))
        self.assertEqual(set(files), {"base.py", "models.py", "__init__.py"})
        self.assertEqual(files["models.py"]["table_names"], ["users", "orders"])

    def test_single_mode_holds_every_class(self):
        content = self.by_path(self.render("single"))["models.py"]["content"]
        self.assertIn("class User(Base):", content)
        self.assertIn("class Order(Base):", content)

    def test_multiple_mode_one_class_per_file(self):
        files = self.by_path(self.render("multiple"))
        self.assertIn("class User(Base):", files["users.py"]["content"])
        self.assertNotIn("class Order(", files["users.py"]["content"])

    def test_snake_case_module_names(self):
        generation = GenerationConfig.from_dict({"mode": "multiple"})
        table = make_table("order_items", [{"name": "a", "type": sa.Integer(), "nullable": True}])
        files = Renderer(generation).render([table])
        self.assertIn("order_items.py", [f["relative_path"] for f in files])

    def test_base_file_suppressed_when_import_supplied(self):
        files = self.by_path(self.render("multiple", base_import="from myapp.db import Base"))
        self.assertNotIn("base.py", files)
        self.assertIn("from myapp.db import Base", files["users.py"]["content"])

    def test_init_suppressed(self):
        files = self.by_path(self.render("multiple", generate_init=False))
        self.assertNotIn("__init__.py", files)

    def test_base_file_not_listed_in_init_when_imported(self):
        files = self.by_path(self.render("multiple", base_import="from myapp.db import Base"))
        self.assertNotIn("Base", files["__init__.py"]["content"].split("__all__")[0]
                         .replace("from myapp.db import Base", ""))

    def test_custom_template_directory(self):
        workdir = tempfile.mkdtemp(prefix="sa_tpl_unit_")
        try:
            template_dir = os.path.join(workdir, "templates")
            shutil.copytree(os.path.join(ENGINE_ROOT, "templates"), template_dir)
            base_path = os.path.join(template_dir, "base.py.j2")
            with open(base_path, "w", encoding="utf-8") as handle:
                handle.write("# marker {{ base_class_name }}\n")
            generation = GenerationConfig.from_dict({"mode": "multiple", "tables": ["users"]})
            files = Renderer(generation, template_dir=template_dir).render(users_and_orders())
            content = next(f["content"] for f in files if f["relative_path"] == "base.py")
            self.assertEqual(content, "# marker Base\n")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def test_missing_template_raises_clearly(self):
        workdir = tempfile.mkdtemp(prefix="sa_tpl_bad_")
        try:
            os.makedirs(os.path.join(workdir, "templates"))
            generation = GenerationConfig.from_dict({"mode": "multiple", "tables": ["users"]})
            with self.assertRaises(Exception):
                Renderer(generation, template_dir=os.path.join(workdir, "templates")).render(
                    users_and_orders())
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


class TestRenderedSourceQuality(unittest.TestCase):
    def setUp(self):
        generation = GenerationConfig.from_dict({"mode": "multiple", "tables": ["users", "orders"]})
        self.files = {f["relative_path"]: f["content"]
                      for f in Renderer(generation).render(users_and_orders())}

    def test_class_and_tablename(self):
        content = self.files["users.py"]
        self.assertIn('class User(Base):', content)
        self.assertIn('__tablename__ = "users"', content)

    def test_every_imported_sqlalchemy_symbol_is_used(self):
        for path, content in self.files.items():
            if path == "__init__.py" or path == "base.py":
                continue
            lines = [l for l in content.splitlines() if l.startswith("from sqlalchemy import ")]
            if not lines:
                continue
            body = content.split("class ", 1)[1]
            for symbol in lines[0].split("import ", 1)[1].split(","):
                symbol = symbol.strip()
                with self.subTest(file=path, symbol=symbol):
                    self.assertRegex(body, rf"(?<![A-Za-z_]){re.escape(symbol)}\b")

    def test_no_duplicate_import_lines(self):
        for path, content in self.files.items():
            lines = [l for l in content.splitlines() if l.startswith(("import ", "from "))]
            self.assertEqual(len(lines), len(set(lines)), f"duplicate imports in {path}")

    def test_no_trailing_whitespace(self):
        for path, content in self.files.items():
            for lineno, line in enumerate(content.splitlines(), 1):
                with self.subTest(file=path, line=lineno):
                    self.assertEqual(line, line.rstrip())

    def test_files_end_with_a_single_newline(self):
        for path, content in self.files.items():
            with self.subTest(file=path):
                self.assertTrue(content.endswith("\n"))
                self.assertFalse(content.endswith("\n\n"))

    def test_init_all_matches_imports(self):
        content = self.files["__init__.py"]
        exported = re.findall(r'"([A-Za-z_]+)"', content.split("__all__")[1])
        imported = re.findall(r"^from \.\w+ import (.+)$", content, re.MULTILINE)
        names = {n.strip() for line in imported for n in line.split(",")}
        self.assertEqual(set(exported), names)


if __name__ == "__main__":
    unittest.main()
