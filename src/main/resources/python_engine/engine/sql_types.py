"""Parse SQL type strings into SQLAlchemy type instances.

Used for offline metadata (`generation.mock_metadata`) so the engine and its
tests never need a live database. Deliberately covers the common MySQL and
PostgreSQL types rather than the full SQL grammar; unknown names raise a
TypeError that the caller reports as a warning.
"""

import re

import sqlalchemy as sa
from sqlalchemy.dialects import mysql as mysql_dialect
from sqlalchemy.dialects import postgresql as pg_dialect

_TYPE_PATTERN = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_. ]*?)\s*(?:\(([^)]*)\))?\s*$", re.IGNORECASE)

# Ordered: more specific names first so "DOUBLE PRECISION" wins over "DOUBLE".
_GENERIC = {
    "DOUBLE PRECISION": sa.Double,
    "CHARACTER VARYING": sa.VARCHAR,
    "TIMESTAMP WITH TIME ZONE": sa.DateTime,
    "TIMESTAMP WITHOUT TIME ZONE": sa.DateTime,
    "TIME WITH TIME ZONE": sa.Time,
    "TIME WITHOUT TIME ZONE": sa.Time,
    "VARCHAR": sa.VARCHAR,
    "NVARCHAR": sa.NVARCHAR,
    "CHAR": sa.CHAR,
    "NCHAR": sa.NCHAR,
    "CHARACTER": sa.CHAR,
    "TEXT": sa.Text,
    "CLOB": sa.Text,
    "INTEGER": sa.Integer,
    "INT": sa.Integer,
    "BIGINT": sa.BigInteger,
    "SMALLINT": sa.SmallInteger,
    "BOOLEAN": sa.Boolean,
    "BOOL": sa.Boolean,
    "FLOAT": sa.Float,
    "REAL": sa.Float,
    "DOUBLE": sa.Double,
    "NUMERIC": sa.Numeric,
    "DECIMAL": sa.Numeric,
    "DATE": sa.Date,
    "DATETIME": sa.DateTime,
    "TIMESTAMP": sa.DateTime,
    "TIMESTAMPTZ": sa.DateTime,
    "TIME": sa.Time,
    "TIMETZ": sa.Time,
    "INTERVAL": sa.Interval,
    "BLOB": sa.LargeBinary,
    "BINARY": sa.LargeBinary,
    "VARBINARY": sa.LargeBinary,
    "JSON": sa.JSON,
    "UUID": sa.Uuid,
    "ENUM": sa.Enum,
}

_MYSQL = {
    "TINYINT": mysql_dialect.TINYINT,
    "MEDIUMINT": mysql_dialect.MEDIUMINT,
    "TINYTEXT": mysql_dialect.TINYTEXT,
    "MEDIUMTEXT": mysql_dialect.MEDIUMTEXT,
    "LONGTEXT": mysql_dialect.LONGTEXT,
    "TINYBLOB": mysql_dialect.TINYBLOB,
    "MEDIUMBLOB": mysql_dialect.MEDIUMBLOB,
    "LONGBLOB": mysql_dialect.LONGBLOB,
    "YEAR": mysql_dialect.YEAR,
    "BIT": mysql_dialect.BIT,
    "SET": mysql_dialect.SET,
}

_POSTGRESQL = {
    "JSONB": pg_dialect.JSONB,
    "BYTEA": pg_dialect.BYTEA,
    "INET": pg_dialect.INET,
    "CIDR": pg_dialect.CIDR,
    "MACADDR": pg_dialect.MACADDR,
    "MACADDR8": pg_dialect.MACADDR8,
    "MONEY": pg_dialect.MONEY,
    "TSVECTOR": pg_dialect.TSVECTOR,
    # This SQLAlchemy version exposes no postgresql.XML class; text is equivalent
    # for model-generation purposes.
    "XML": sa.Text,
    "HSTORE": pg_dialect.HSTORE,
    "SERIAL": sa.Integer,
    "BIGSERIAL": sa.BigInteger,
    "SMALLSERIAL": sa.SmallInteger,
    "UUID": pg_dialect.UUID,
    # PG bit strings carry raw bits, not integers.
    "BIT": sa.LargeBinary,
    "VARBIT": sa.LargeBinary,
}

_DIALECT_TABLES = {
    "mysql": _MYSQL,
    "mariadb": _MYSQL,
    "postgresql": _POSTGRESQL,
}

# Types whose single argument is a character length.
_STRING_TYPES = {sa.VARCHAR, sa.NVARCHAR, sa.CHAR, sa.NCHAR, sa.String}
# Types taking (precision, scale).
_NUMERIC_TYPES = {sa.Numeric}
# Types taking a list of quoted labels.
_LABEL_TYPES = {sa.Enum}
if hasattr(mysql_dialect, "SET"):
    _LABEL_TYPES.add(mysql_dialect.SET)

# Names whose SQLAlchemy class takes a `timezone` flag. Kept as explicit sets
# because "TIMESTAMP WITHOUT TIME ZONE" contains the substring "TIME ZONE".
_TIMESTAMP_NAMES = {
    "TIMESTAMP", "TIMESTAMPTZ", "DATETIME",
    "TIMESTAMP WITH TIME ZONE", "TIMESTAMP WITHOUT TIME ZONE",
}
_TIME_NAMES = {"TIME", "TIMETZ", "TIME WITH TIME ZONE", "TIME WITHOUT TIME ZONE"}


class UnknownSqlType(TypeError):
    """Raised when a SQL type string cannot be resolved."""


def _split_args(raw):
    if raw is None:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _unquote(value):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def _coerce_number(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def type_from_string(text, dialect=""):
    """`"VARCHAR(50)"` -> `sa.VARCHAR(50)`; `"JSONB"` -> `pg.JSONB()`."""
    if text is None:
        raise UnknownSqlType("type string is empty")
    if isinstance(text, sa.types.TypeEngine):
        return text

    match = _TYPE_PATTERN.match(str(text))
    if not match:
        raise UnknownSqlType(f"Cannot parse SQL type {text!r}")

    raw_name = match.group(1).strip()
    # Allow an explicit dialect qualifier: "postgresql.JSONB"
    explicit_dialect = ""
    if "." in raw_name:
        explicit_dialect, _, raw_name = raw_name.rpartition(".")
        explicit_dialect = explicit_dialect.strip().lower()
    name = re.sub(r"\s+", " ", raw_name).upper()
    args = _split_args(match.group(2))

    tz = name.endswith("TZ") or bool(re.search(r"\bWITH\s+TIME\s*ZONE\b", str(text), re.IGNORECASE))

    table = _DIALECT_TABLES.get(explicit_dialect or dialect, {})
    cls = table.get(name) or _GENERIC.get(name) or _POSTGRESQL.get(name) or _MYSQL.get(name)
    if cls is None:
        raise UnknownSqlType(f"Unknown SQL type {name!r} (dialect={dialect or 'generic'})")

    if name in _TIMESTAMP_NAMES or name in _TIME_NAMES:
        if not _accepts_kwarg(cls, "timezone"):
            return cls()
        return cls(timezone=tz)

    if not args:
        return cls()

    if cls in _LABEL_TYPES or name in ("ENUM", "SET"):
        return cls(*[_unquote(a) for a in args])
    if cls in _NUMERIC_TYPES or name in ("NUMERIC", "DECIMAL"):
        numbers = [_coerce_number(a) for a in args]
        numbers = [n for n in numbers if n is not None]
        if len(numbers) >= 2:
            return cls(precision=numbers[0], scale=numbers[1])
        if len(numbers) == 1:
            return cls(precision=numbers[0])
        return cls()
    if cls in _STRING_TYPES or name in ("TINYINT", "BIT"):
        number = _coerce_number(args[0])
        if number is None:
            return cls()
        if name == "TINYINT":
            return cls(display_width=number) if _accepts_kwarg(cls, "display_width") else cls()
        return cls(length=number) if _accepts_kwarg(cls, "length") else cls(number)

    number = _coerce_number(args[0])
    if number is not None and _accepts_kwarg(cls, "length"):
        return cls(length=number)
    try:
        return cls()
    except TypeError:
        raise UnknownSqlType(f"Cannot instantiate {name!r} with args {args}") from None


def _accepts_kwarg(cls, name):
    import inspect

    try:
        return name in inspect.signature(cls.__init__).parameters
    except (TypeError, ValueError):
        return False
