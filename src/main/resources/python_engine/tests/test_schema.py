"""Reflection dict -> TableMeta conversion, and mapped_column() rendering."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from engine.schema import build_table_meta
from engine.type_mapper import MapperContext


def col(name, type_, nullable=True, **extra):
    entry = {"name": name, "type": type_, "nullable": nullable}
    entry.update(extra)
    return entry


class TestColumnBasics(unittest.TestCase):
    def build(self, columns, **kwargs):
        kwargs.setdefault("primary_key", {"constrained_columns": []})
        return build_table_meta("t", columns, ctx=MapperContext(dialect="mysql"), **kwargs)

    def test_nullable_column_uses_optional_annotation(self):
        table = self.build([col("email", sa.String(100), nullable=True)])
        column = table.columns[0]
        self.assertEqual(column.annotation, "Mapped[str | None]")
        # nullable defaults to True, so it must not be stated.
        self.assertNotIn("nullable", column.mapped_column_args())

    def test_non_nullable_column_states_nullable_false(self):
        table = self.build([col("name", sa.String(50), nullable=False)])
        column = table.columns[0]
        self.assertEqual(column.annotation, "Mapped[str]")
        self.assertIn("nullable=False", column.mapped_column_args())

    def test_primary_key_is_never_nullable(self):
        table = self.build(
            [col("id", sa.Integer(), nullable=True, autoincrement=True)],
            primary_key={"constrained_columns": ["id"]},
        )
        column = table.columns[0]
        self.assertTrue(column.primary_key)
        self.assertFalse(column.nullable)
        self.assertEqual(column.annotation, "Mapped[int]")

    def test_autoincrement_primary_key_matches_style_guide(self):
        table = self.build(
            [col("id", sa.Integer(), nullable=False, autoincrement=True)],
            primary_key={"constrained_columns": ["id"]},
        )
        args = table.columns[0].mapped_column_args(emit_comments=False)
        self.assertEqual(args, ["primary_key=True", "autoincrement=True"])
        # The style guide omits Integer for an int PK: the annotation implies it.
        self.assertNotIn("Integer", args)

    def test_bigint_primary_key_keeps_its_type(self):
        table = self.build(
            [col("id", sa.BigInteger(), nullable=False, autoincrement=True)],
            primary_key={"constrained_columns": ["id"]},
        )
        args = table.columns[0].mapped_column_args(emit_comments=False)
        self.assertIn("BigInteger", args)

    def test_length_carrying_string_is_emitted(self):
        table = self.build([col("name", sa.String(50), nullable=False)])
        args = table.columns[0].mapped_column_args(emit_comments=False)
        self.assertEqual(args, ["String(50)", "nullable=False"])


class TestMappedColumnArgumentOrder(unittest.TestCase):
    """`mapped_column` takes (name, type, *SchemaItem). Getting this wrong is a
    runtime ArgumentError that `ast.parse` cannot catch."""

    def test_explicit_name_comes_before_type(self):
        table = build_table_meta(
            "t",
            [col("userName", sa.String(50), nullable=False)],
            primary_key={"constrained_columns": []},
            ctx=MapperContext(),
        )
        args = table.columns[0].mapped_column_args(emit_comments=False)
        self.assertEqual(args[0], "'userName'")
        self.assertEqual(args[1], "String(50)")

    def test_foreign_key_comes_after_type(self):
        table = build_table_meta(
            "orders",
            [col("id", sa.Integer(), nullable=False, autoincrement=True),
             col("user_id", sa.Integer(), nullable=False)],
            primary_key={"constrained_columns": ["id"]},
            foreign_keys=[{"constrained_columns": ["user_id"], "referred_table": "users",
                           "referred_columns": ["id"]}],
            ctx=MapperContext(),
        )
        fk_column = table.column("user_id")
        args = fk_column.mapped_column_args(emit_comments=False)
        self.assertEqual(args, ['ForeignKey("users.id")', "nullable=False"])
        self.assertIn("ForeignKey", fk_column.sa_imports)

    def test_renamed_foreign_key_uses_database_names(self):
        table = build_table_meta(
            "orders",
            [col("userID", sa.Integer(), nullable=False)],
            primary_key={"constrained_columns": []},
            foreign_keys=[{"constrained_columns": ["userID"], "referred_table": "users",
                           "referred_columns": ["id"]}],
            ctx=MapperContext(),
        )
        column = table.columns[0]
        self.assertEqual(column.attr_name, "user_id")
        self.assertIn('ForeignKey("users.id")', column.mapped_column_args(emit_comments=False))
        self.assertEqual(column.mapped_column_args(emit_comments=False)[0], "'userID'")

    def test_server_default_uses_text_wrapper(self):
        table = build_table_meta(
            "t",
            [col("created_at", sa.DateTime(), nullable=False, default="CURRENT_TIMESTAMP")],
            primary_key={"constrained_columns": []},
            ctx=MapperContext(),
        )
        column = table.columns[0]
        self.assertIn("server_default=text('CURRENT_TIMESTAMP')", column.mapped_column_args())
        self.assertTrue(column.needs_text_import)

    def test_autoincrement_pk_does_not_also_emit_server_default(self):
        table = build_table_meta(
            "t",
            [col("id", sa.Integer(), nullable=False, autoincrement=True,
                 default="nextval('t_id_seq'::regclass)")],
            primary_key={"constrained_columns": ["id"]},
            ctx=MapperContext(),
        )
        args = table.columns[0].mapped_column_args(emit_comments=False)
        self.assertNotIn("server_default", " ".join(args))

    def test_comments_are_optional(self):
        table = build_table_meta(
            "t",
            [col("name", sa.String(10), nullable=True, comment="the name")],
            primary_key={"constrained_columns": []},
            ctx=MapperContext(),
        )
        column = table.columns[0]
        self.assertIn("comment='the name'", column.mapped_column_args(emit_comments=True))
        self.assertNotIn("comment", " ".join(column.mapped_column_args(emit_comments=False)))


class TestAutoincrementDetection(unittest.TestCase):
    def build_pk(self, column_dict):
        return build_table_meta(
            "t", [column_dict], primary_key={"constrained_columns": ["id"]}, ctx=MapperContext()
        ).columns[0]

    def test_explicit_flag(self):
        self.assertTrue(self.build_pk(col("id", sa.Integer(), nullable=False, autoincrement=True)).autoincrement)

    def test_explicit_false_flag(self):
        column = self.build_pk(col("id", sa.Integer(), nullable=False, autoincrement=False,
                                   extra="auto_increment"))
        self.assertFalse(column.autoincrement)

    def test_mysql_extra(self):
        self.assertTrue(self.build_pk(col("id", sa.Integer(), nullable=False,
                                          extra="auto_increment")).autoincrement)

    def test_postgres_sequence_default(self):
        column = self.build_pk(col("id", sa.Integer(), nullable=False,
                                   default="nextval('t_id_seq'::regclass)"))
        self.assertTrue(column.autoincrement)

    def test_identity_dict(self):
        self.assertTrue(self.build_pk(col("id", sa.Integer(), nullable=False,
                                          identity={"always": True})).autoincrement)

    def test_non_pk_never_autoincrements(self):
        table = build_table_meta(
            "t",
            [col("id", sa.Integer(), nullable=False, autoincrement=True),
             col("other", sa.Integer(), nullable=False, extra="auto_increment")],
            primary_key={"constrained_columns": ["id"]},
            ctx=MapperContext(),
        )
        self.assertFalse(table.column("other").autoincrement)


class TestConstraintsAndIndexes(unittest.TestCase):
    def test_single_column_unique_index_sets_flag(self):
        table = build_table_meta(
            "t",
            [col("email", sa.String(100), nullable=True)],
            primary_key={"constrained_columns": []},
            indexes=[{"name": "uq_email", "column_names": ["email"], "unique": True}],
            ctx=MapperContext(),
        )
        column = table.columns[0]
        self.assertTrue(column.unique)
        self.assertIn("unique=True", column.mapped_column_args(emit_comments=False))
        self.assertNotIn("index=True", column.mapped_column_args(emit_comments=False))

    def test_single_column_plain_index_sets_flag(self):
        table = build_table_meta(
            "t",
            [col("created_at", sa.DateTime(), nullable=False)],
            primary_key={"constrained_columns": []},
            indexes=[{"name": "ix_created", "column_names": ["created_at"], "unique": False}],
            ctx=MapperContext(),
        )
        self.assertTrue(table.columns[0].index)
        self.assertIn("index=True", table.columns[0].mapped_column_args(emit_comments=False))

    def test_multi_column_unique_goes_to_table_args(self):
        table = build_table_meta(
            "t",
            [col("a", sa.Integer(), nullable=False), col("b", sa.Integer(), nullable=False)],
            primary_key={"constrained_columns": []},
            unique_constraints=[{"name": "uq_ab", "column_names": ["a", "b"]}],
            ctx=MapperContext(),
        )
        self.assertFalse(table.column("a").unique)
        self.assertEqual(len(table.table_args), 1)
        self.assertIn("UniqueConstraint('a', 'b', name='uq_ab')", table.table_args[0])
        self.assertIn("UniqueConstraint", table.extra_sa_imports)

    def test_multi_column_index_goes_to_table_args(self):
        table = build_table_meta(
            "t",
            [col("a", sa.Integer()), col("b", sa.Integer())],
            primary_key={"constrained_columns": []},
            indexes=[{"name": "ix_ab", "column_names": ["a", "b"], "unique": False}],
            ctx=MapperContext(),
        )
        self.assertIn("Index('a', 'b', name='ix_ab')", table.table_args[0])
        self.assertIn("Index", table.extra_sa_imports)

    def test_schema_is_last_table_arg(self):
        # SQLAlchemy requires the options dict to come after constraints.
        table = build_table_meta(
            "t",
            [col("a", sa.Integer())],
            primary_key={"constrained_columns": []},
            unique_constraints=[{"name": "uq", "column_names": ["a", "a2"]}],
            schema="reporting",
            ctx=MapperContext(),
        )
        self.assertTrue(table.table_args[-1].startswith("{'schema'"))

    def test_composite_primary_key(self):
        table = build_table_meta(
            "t",
            [col("a", sa.Integer(), nullable=False), col("b", sa.Integer(), nullable=False)],
            primary_key={"constrained_columns": ["a", "b"]},
            ctx=MapperContext(),
        )
        self.assertTrue(table.has_composite_pk)
        self.assertTrue(table.column("a").primary_key)
        self.assertTrue(table.column("b").primary_key)

    def test_primary_key_given_as_plain_list(self):
        table = build_table_meta(
            "t", [col("id", sa.Integer(), nullable=False)], primary_key=["id"], ctx=MapperContext()
        )
        self.assertTrue(table.columns[0].primary_key)


class TestNamingIntegration(unittest.TestCase):
    def test_table_prefix_stripping(self):
        table = build_table_meta(
            "t_user_profile", [col("id", sa.Integer())], primary_key=["id"],
            prefixes=["t_"], ctx=MapperContext(),
        )
        self.assertEqual(table.class_name, "UserProfile")
        self.assertEqual(table.module_name, "user_profile")
        # __tablename__ always keeps the real database name.
        self.assertEqual(table.name, "t_user_profile")

    def test_class_name_override(self):
        table = build_table_meta(
            "users", [col("id", sa.Integer())], primary_key=["id"],
            ctx=MapperContext(), class_name_override="Account",
        )
        self.assertEqual(table.class_name, "Account")

    def test_keyword_column_is_renamed_and_name_preserved(self):
        table = build_table_meta(
            "t", [col("class", sa.String(10))], primary_key={"constrained_columns": []},
            ctx=MapperContext(),
        )
        column = table.columns[0]
        self.assertEqual(column.attr_name, "class_")
        self.assertTrue(column.name_differs)
        self.assertEqual(column.mapped_column_args(emit_comments=False)[0], "'class'")

    def test_colliding_normalised_names_are_disambiguated(self):
        # "userId" and "user_id" both normalise to "user_id".
        table = build_table_meta(
            "t",
            [col("userId", sa.Integer()), col("user_id", sa.Integer())],
            primary_key={"constrained_columns": []},
            ctx=MapperContext(),
        )
        names = [c.attr_name for c in table.columns]
        self.assertEqual(len(set(names)), 2)
        self.assertEqual(names[0], "user_id")
        self.assertEqual(names[1], "user_id_2")

    def test_singularisation_is_configurable(self):
        kwargs = dict(primary_key={"constrained_columns": []}, ctx=MapperContext())
        self.assertEqual(build_table_meta("users", [col("a", sa.Integer())], **kwargs).class_name, "User")
        self.assertEqual(
            build_table_meta("users", [col("a", sa.Integer())], singularize_tables=False, **kwargs).class_name,
            "Users",
        )


class TestComments(unittest.TestCase):
    def test_table_comment_kept(self):
        table = build_table_meta(
            "t", [col("a", sa.Integer())], primary_key={"constrained_columns": []},
            comment="系统用户表", ctx=MapperContext(),
        )
        self.assertEqual(table.comment, "系统用户表")

    def test_comments_can_be_suppressed(self):
        table = build_table_meta(
            "t", [col("a", sa.Integer(), comment="col comment")],
            primary_key={"constrained_columns": []}, comment="table comment",
            include_comments=False, ctx=MapperContext(),
        )
        self.assertIsNone(table.comment)
        self.assertIsNone(table.columns[0].comment)


class TestUnmappedColumns(unittest.TestCase):
    def test_unknown_type_is_flagged_with_a_note(self):
        class Exotic(sa.types.TypeEngine):
            __visit_name__ = "EXOTIC"

        table = build_table_meta(
            "t", [col("weird", Exotic())], primary_key={"constrained_columns": []},
            ctx=MapperContext(),
        )
        column = table.columns[0]
        self.assertTrue(column.unmapped)
        self.assertEqual(column.python_type, "Any")
        self.assertEqual(column.annotation, "Mapped[Any | None]")
        self.assertTrue(column.type_notes)
        # The note is rendered as a source comment so the user sees it.
        self.assertIn("EXOTIC", column.type_notes[0])


class TestMySQLSpecifics(unittest.TestCase):
    def test_tinyint_one_becomes_bool(self):
        table = build_table_meta(
            "t", [col("is_active", mysql.TINYINT(display_width=1), nullable=False)],
            primary_key={"constrained_columns": []}, ctx=MapperContext(dialect="mysql"),
        )
        self.assertEqual(table.columns[0].python_type, "bool")
        self.assertEqual(table.columns[0].annotation, "Mapped[bool]")


if __name__ == "__main__":
    unittest.main()
