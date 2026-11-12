"""SQL type string parsing (used by the offline / mock metadata path)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlalchemy as sa
from sqlalchemy.dialects import mysql, postgresql

from engine.sql_types import UnknownSqlType, type_from_string


class TestGenericParsing(unittest.TestCase):
    def test_plain_names(self):
        self.assertIsInstance(type_from_string("INTEGER"), sa.Integer)
        self.assertIsInstance(type_from_string("TEXT"), sa.Text)
        self.assertIsInstance(type_from_string("BOOLEAN"), sa.Boolean)
        self.assertIsInstance(type_from_string("DATE"), sa.Date)

    def test_case_insensitive(self):
        self.assertIsInstance(type_from_string("integer"), sa.Integer)
        self.assertIsInstance(type_from_string("Integer"), sa.Integer)

    def test_varchar_length(self):
        result = type_from_string("VARCHAR(50)")
        self.assertIsInstance(result, sa.VARCHAR)
        self.assertEqual(result.length, 50)

    def test_numeric_precision_and_scale(self):
        result = type_from_string("NUMERIC(10,2)")
        self.assertEqual(result.precision, 10)
        self.assertEqual(result.scale, 2)

    def test_decimal_with_spaces(self):
        result = type_from_string("DECIMAL( 12 , 4 )")
        self.assertEqual(result.precision, 12)
        self.assertEqual(result.scale, 4)

    def test_multiword_names(self):
        self.assertIsInstance(type_from_string("DOUBLE PRECISION"), sa.Double)
        self.assertIsInstance(type_from_string("CHARACTER VARYING(30)"), sa.VARCHAR)

    def test_surrounding_whitespace(self):
        self.assertIsInstance(type_from_string("  INTEGER  "), sa.Integer)

    def test_enum_labels(self):
        result = type_from_string("ENUM('draft','paid','shipped')")
        self.assertIsInstance(result, sa.Enum)
        self.assertEqual(list(result.enums), ["draft", "paid", "shipped"])

    def test_double_quoted_enum_labels(self):
        result = type_from_string('ENUM("a","b")')
        self.assertEqual(list(result.enums), ["a", "b"])


class TestTimezoneParsing(unittest.TestCase):
    def test_timestamp_with_time_zone(self):
        result = type_from_string("TIMESTAMP WITH TIME ZONE")
        self.assertTrue(result.timezone)

    def test_timestamptz_shorthand(self):
        result = type_from_string("TIMESTAMPTZ")
        self.assertTrue(getattr(result, "timezone", False))

    def test_plain_timestamp_has_no_timezone(self):
        result = type_from_string("TIMESTAMP")
        self.assertFalse(result.timezone)

    def test_timetz(self):
        result = type_from_string("TIME WITH TIME ZONE")
        self.assertTrue(result.timezone)


class TestDialectResolution(unittest.TestCase):
    def test_mysql_dialect_from_context(self):
        result = type_from_string("TINYINT(1)", "mysql")
        self.assertIsInstance(result, mysql.TINYINT)
        self.assertEqual(result.display_width, 1)

    def test_explicit_dialect_qualifier_overrides_context(self):
        result = type_from_string("postgresql.JSONB", "mysql")
        self.assertIsInstance(result, postgresql.JSONB)

    def test_explicit_qualifier_with_args(self):
        result = type_from_string("mysql.VARCHAR(64)", "postgresql")
        self.assertIsInstance(result, sa.VARCHAR)
        self.assertEqual(result.length, 64)

    def test_postgres_types(self):
        self.assertIsInstance(type_from_string("JSONB", "postgresql"), postgresql.JSONB)
        self.assertIsInstance(type_from_string("BYTEA", "postgresql"), postgresql.BYTEA)
        self.assertIsInstance(type_from_string("INET", "postgresql"), postgresql.INET)
        self.assertIsInstance(type_from_string("UUID", "postgresql"), postgresql.UUID)
        self.assertIsInstance(type_from_string("MONEY", "postgresql"), postgresql.MONEY)

    def test_pg_bit_is_binary_not_mysql_bit(self):
        self.assertIsInstance(type_from_string("BIT", "postgresql"), sa.LargeBinary)
        self.assertIsInstance(type_from_string("BIT(1)", "mysql"), mysql.BIT)

    def test_xml_maps_to_text(self):
        # This SQLAlchemy build has no postgresql.XML class.
        self.assertIsInstance(type_from_string("XML", "postgresql"), sa.Text)

    def test_generic_fallback_for_dialect_specific_name(self):
        # TINYINT is a MySQL type but should still resolve without a dialect hint.
        self.assertIsInstance(type_from_string("TINYINT"), mysql.TINYINT)


class TestPassThroughAndErrors(unittest.TestCase):
    def test_already_a_type_instance(self):
        original = sa.String(20)
        self.assertIs(type_from_string(original), original)

    def test_unknown_type_raises(self):
        with self.assertRaises(UnknownSqlType):
            type_from_string("SOME_MADE_UP_TYPE")

    def test_unparseable_raises(self):
        with self.assertRaises(UnknownSqlType):
            type_from_string("123 NOT A TYPE")

    def test_none_raises(self):
        with self.assertRaises(UnknownSqlType):
            type_from_string(None)


if __name__ == "__main__":
    unittest.main()
