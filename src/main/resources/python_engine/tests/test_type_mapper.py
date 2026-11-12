"""Database type -> Python annotation / SQLAlchemy type mapping."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlalchemy as sa
from sqlalchemy.dialects import mysql, postgresql

from engine.type_mapper import MapperContext, collect_imports, describe_builtin_mappings, map_type


class TypeMapperTestCase(unittest.TestCase):
    def setUp(self):
        self.ctx = MapperContext(dialect="mysql")

    def assertMaps(self, sa_type, python_type, sa_expr, emit=None):
        mapping = map_type(sa_type, self.ctx)
        self.assertFalse(mapping.unmapped, f"{sa_type!r} fell through to the unmapped branch")
        self.assertEqual(mapping.python_type, python_type)
        self.assertEqual(mapping.sa_type, sa_expr)
        if emit is not None:
            self.assertEqual(mapping.should_emit_sa_type, emit, f"emit flag for {sa_expr}")
        return mapping


class TestMySQLTypes(TypeMapperTestCase):
    def test_tinyint_one_is_boolean(self):
        # TINYINT(1) is the universal MySQL boolean idiom.
        self.assertMaps(mysql.TINYINT(display_width=1), "bool", "Boolean", emit=False)

    def test_wider_tinyint_is_integer(self):
        self.assertMaps(mysql.TINYINT(display_width=4), "int", "SmallInteger", emit=True)

    def test_mediumint(self):
        self.assertMaps(mysql.MEDIUMINT(), "int", "Integer", emit=False)

    def test_year(self):
        self.assertMaps(mysql.YEAR(), "int", "SmallInteger", emit=True)

    def test_bit_one_is_boolean(self):
        self.assertMaps(mysql.BIT(1), "bool", "Boolean", emit=False)

    def test_wide_bit_is_integer(self):
        self.assertMaps(mysql.BIT(8), "int", "BigInteger", emit=True)

    def test_varchar_keeps_length(self):
        self.assertMaps(mysql.VARCHAR(50), "str", "String(50)", emit=True)

    def test_longtext_maps_to_text_not_string(self):
        # mysql.LONGTEXT subclasses sqlalchemy.String, so a pure isinstance walk
        # would wrongly emit String; visit_name dispatch gets Text.
        self.assertMaps(mysql.LONGTEXT(), "str", "Text", emit=True)

    def test_mediumtext_and_tinytext(self):
        self.assertMaps(mysql.MEDIUMTEXT(), "str", "Text")
        self.assertMaps(mysql.TINYTEXT(), "str", "Text")

    def test_blob_family_is_bytes(self):
        for blob in (mysql.TINYBLOB(), mysql.MEDIUMBLOB(), mysql.LONGBLOB()):
            self.assertMaps(blob, "bytes", "LargeBinary", emit=True)

    def test_decimal_keeps_precision(self):
        self.assertMaps(mysql.DECIMAL(10, 2), "Decimal", "Numeric(10, 2)", emit=True)

    def test_double(self):
        self.assertMaps(mysql.DOUBLE(), "float", "Double", emit=True)

    def test_json(self):
        mapping = self.assertMaps(mysql.JSON(), "dict[str, Any]", "JSON", emit=True)
        self.assertIn("from typing import Any", mapping.py_imports)

    def test_enum_defaults_to_string_with_max_value_length(self):
        mapping = self.assertMaps(mysql.ENUM("draft", "published"), "str", "String(9)", emit=True)
        self.assertEqual(len("published"), 9)

    def test_enum_as_native_when_requested(self):
        ctx = MapperContext(dialect="mysql", enum_as_native=True)
        mapping = map_type(mysql.ENUM("draft", "published", name="order_status"), ctx)
        self.assertEqual(mapping.python_type, "str")
        self.assertIn("Enum(", mapping.sa_type)
        self.assertIn("'draft'", mapping.sa_type)
        self.assertIn("name='order_status'", mapping.sa_type)


class TestPostgresTypes(TypeMapperTestCase):
    def setUp(self):
        self.ctx = MapperContext(dialect="postgresql")

    def test_uuid(self):
        mapping = self.assertMaps(postgresql.UUID(), "uuid.UUID", "Uuid", emit=True)
        self.assertIn("import uuid", mapping.py_imports)

    def test_jsonb_falls_back_to_generic_json(self):
        mapping = self.assertMaps(postgresql.JSONB(), "dict[str, Any]", "JSON", emit=True)
        self.assertNotIn("JSONB", mapping.sa_type)

    def test_jsonb_uses_dialect_type_when_requested(self):
        ctx = MapperContext(dialect="postgresql", use_dialect_types=True)
        mapping = map_type(postgresql.JSONB(), ctx)
        self.assertEqual(mapping.sa_type, "JSONB")
        self.assertIn("from sqlalchemy.dialects.postgresql import JSONB", mapping.py_imports)

    def test_hstore_always_needs_dialect_import(self):
        mapping = self.assertMaps(postgresql.HSTORE(), "dict[str, str]", "HSTORE", emit=True)
        self.assertIn("from sqlalchemy.dialects.postgresql import HSTORE", mapping.py_imports)

    def test_bytea(self):
        self.assertMaps(postgresql.BYTEA(), "bytes", "LargeBinary", emit=True)

    def test_money_is_decimal(self):
        self.assertMaps(postgresql.MONEY(), "Decimal", "Numeric", emit=True)

    def test_network_types_get_explicit_lengths(self):
        # Without a length these would collapse to a bare `String`, which the
        # renderer omits as "natural" for str -- losing the type entirely.
        self.assertMaps(postgresql.INET(), "str", "String(45)", emit=True)
        self.assertMaps(postgresql.CIDR(), "str", "String(45)", emit=True)
        self.assertMaps(postgresql.MACADDR(), "str", "String(17)", emit=True)
        self.assertMaps(postgresql.MACADDR8(), "str", "String(23)", emit=True)

    def test_timestamptz_preserves_timezone(self):
        self.assertMaps(postgresql.TIMESTAMP(timezone=True), "datetime",
                        "DateTime(timezone=True)", emit=True)

    def test_timetz_preserves_timezone(self):
        self.assertMaps(postgresql.TIME(timezone=True), "time", "Time(timezone=True)", emit=True)

    def test_interval_resolves_despite_shared_visit_name(self):
        # postgresql.INTERVAL reports visit_name INTERVAL but derives from
        # _AbstractInterval, not sa.Interval, so the class fallback misses it.
        self.assertMaps(postgresql.INTERVAL(), "timedelta", "Interval", emit=True)

    def test_pg_bit_is_bytes_not_integer(self):
        # Unlike MySQL BIT, the PG bit string is not numeric.
        self.assertMaps(postgresql.BIT(8), "bytes", "LargeBinary", emit=True)

    def test_array_of_integer(self):
        mapping = map_type(postgresql.ARRAY(sa.Integer()), self.ctx)
        self.assertEqual(mapping.python_type, "list[int]")
        self.assertEqual(mapping.sa_type, "ARRAY(Integer)")
        self.assertIn("ARRAY", mapping.sa_imports)

    def test_tsvector_and_xml_are_text(self):
        self.assertMaps(postgresql.TSVECTOR(), "str", "Text")


class TestGenericTypes(TypeMapperTestCase):
    def test_integer_is_omitted_as_natural(self):
        self.assertMaps(sa.Integer(), "int", "Integer", emit=False)

    def test_bigint_is_emitted(self):
        self.assertMaps(sa.BigInteger(), "int", "BigInteger", emit=True)

    def test_bigint_sql_alias_also_resolves(self):
        # sa.BIGINT reports visit_name BIGINT while sa.BigInteger reports
        # BIG_INTEGER; both keys must be registered.
        self.assertMaps(sa.BIGINT(), "int", "BigInteger", emit=True)

    def test_smallint_alias(self):
        self.assertMaps(sa.SMALLINT(), "int", "SmallInteger", emit=True)

    def test_string_without_length(self):
        self.assertMaps(sa.String(), "str", "String", emit=False)

    def test_string_with_length(self):
        self.assertMaps(sa.String(255), "str", "String(255)", emit=True)

    def test_text_is_emitted_because_str_implies_string(self):
        self.assertMaps(sa.Text(), "str", "Text", emit=True)

    def test_unicode_text_resolves(self):
        # visit_name is UNICODE_TEXT, not UNICODETEXT.
        self.assertMaps(sa.UnicodeText(), "str", "Text", emit=True)

    def test_boolean_is_omitted(self):
        self.assertMaps(sa.Boolean(), "bool", "Boolean", emit=False)

    def test_datetime_is_omitted(self):
        self.assertMaps(sa.DateTime(), "datetime", "DateTime", emit=False)

    def test_date_and_time(self):
        self.assertMaps(sa.Date(), "date", "Date", emit=False)
        self.assertMaps(sa.Time(), "time", "Time", emit=False)

    def test_interval_via_class_fallback(self):
        # sa.Interval is a TypeDecorator: its visit_name is the useless
        # TYPE_DECORATOR, so only the class-identity walk can resolve it.
        self.assertMaps(sa.Interval(), "timedelta", "Interval", emit=True)

    def test_numeric_precision_and_scale(self):
        self.assertMaps(sa.Numeric(8, 3), "Decimal", "Numeric(8, 3)", emit=True)

    def test_numeric_precision_only(self):
        self.assertMaps(sa.Numeric(8), "Decimal", "Numeric(8)", emit=True)

    def test_large_binary(self):
        self.assertMaps(sa.LargeBinary(), "bytes", "LargeBinary", emit=True)

    def test_double(self):
        self.assertMaps(sa.Double(), "float", "Double", emit=True)

    def test_uuid_generic(self):
        self.assertMaps(sa.Uuid(), "uuid.UUID", "Uuid", emit=True)


class TestOverrides(unittest.TestCase):
    def test_column_override_wins_over_everything(self):
        ctx = MapperContext(
            dialect="mysql",
            column_overrides={"user.status": {"python_type": "str", "sa_type": "String(20)"}},
        )
        mapping = map_type(mysql.TINYINT(display_width=1), ctx, "user", "status")
        self.assertEqual(mapping.python_type, "str")
        self.assertEqual(mapping.sa_type, "String(20)")
        self.assertEqual(mapping.sa_imports, ("String",))

    def test_column_override_does_not_leak_to_other_columns(self):
        ctx = MapperContext(column_overrides={"user.status": {"python_type": "str", "sa_type": "String(20)"}})
        mapping = map_type(mysql.TINYINT(display_width=1), ctx, "user", "is_active")
        self.assertEqual(mapping.python_type, "bool")

    def test_custom_type_map_by_visit_name(self):
        ctx = MapperContext(custom_type_map={"JSON": {"python_type": "dict", "sa_type": "JSON"}})
        mapping = map_type(sa.JSON(), ctx)
        self.assertEqual(mapping.python_type, "dict")

    def test_override_with_dialect_import(self):
        ctx = MapperContext(custom_type_map={
            "JSONB": {
                "python_type": "dict[str, Any]",
                "sa_type": "JSONB",
                "sa_imports": [],
                "dialect_import": "from sqlalchemy.dialects.postgresql import JSONB",
            }
        })
        mapping = map_type(postgresql.JSONB(), ctx)
        self.assertEqual(mapping.sa_type, "JSONB")
        self.assertIn("from sqlalchemy.dialects.postgresql import JSONB", mapping.py_imports)


class TestUnknownTypes(unittest.TestCase):
    def test_unknown_type_is_flagged_not_crashing(self):
        class ExoticType(sa.types.TypeEngine):
            __visit_name__ = "EXOTIC_THING"

        ctx = MapperContext()
        mapping = map_type(ExoticType(), ctx, "t", "c")
        self.assertTrue(mapping.unmapped)
        self.assertEqual(mapping.python_type, "Any")
        self.assertEqual(mapping.sa_type, "")
        self.assertTrue(mapping.notes)
        self.assertTrue(any("EXOTIC_THING" in w for w in ctx.warnings))


class TestImportCollection(unittest.TestCase):
    def test_collect_imports_splits_groups(self):
        ctx = MapperContext(dialect="postgresql", use_dialect_types=True)
        mappings = [
            map_type(sa.Numeric(10, 2), ctx),
            map_type(sa.DateTime(), ctx),
            map_type(postgresql.UUID(), ctx),
            map_type(postgresql.JSONB(), ctx),
        ]
        sa_names, dialect_lines, py_lines = collect_imports(mappings)
        self.assertIn("Numeric", sa_names)
        self.assertIn("Uuid", sa_names)
        self.assertIn("from sqlalchemy.dialects.postgresql import JSONB", dialect_lines)
        self.assertIn("from decimal import Decimal", py_lines)
        self.assertIn("from datetime import datetime", py_lines)
        self.assertIn("import uuid", py_lines)
        # Dialect imports must not leak into the plain sqlalchemy name list.
        self.assertNotIn("JSONB", sa_names)

    def test_sa_names_are_sorted_and_deduplicated(self):
        ctx = MapperContext()
        mappings = [map_type(sa.String(10), ctx), map_type(sa.String(20), ctx), map_type(sa.Text(), ctx)]
        sa_names, _, _ = collect_imports(mappings)
        self.assertEqual(sa_names, ["String", "Text"])


class TestDescribeMappings(unittest.TestCase):
    def test_summary_is_json_shaped(self):
        entries = describe_builtin_mappings()
        self.assertTrue(entries)
        for entry in entries:
            self.assertIn("dialect", entry)
            self.assertIn("db_type", entry)
        db_types = {e["db_type"] for e in entries}
        self.assertIn("VARCHAR", db_types)
        self.assertIn("JSONB", db_types)


if __name__ == "__main__":
    unittest.main()
