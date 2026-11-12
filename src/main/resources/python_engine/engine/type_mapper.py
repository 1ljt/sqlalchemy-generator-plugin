"""Database column type -> Python annotation + SQLAlchemy type expression.

Lookup is keyed on the type's `__visit_name__` (the discriminator SQLAlchemy's
own compiler uses) combined with the dialect it came from. This is deliberately
*not* an `isinstance` walk: `mysql.LONGTEXT` inherits from `sqlalchemy.String`
rather than `Text`, and `mysql.BIT` / `postgresql.BIT` collide by class name but
mean different things, so class hierarchy alone mis-maps real reflected schemas.

`__visit_name__` has two traps this module works around:
  * `BigInteger` reports `BIG_INTEGER` while the SQL alias `BIGINT` reports
    `BIGINT` -- both keys are registered.
  * every `TypeDecorator` (including `Interval`) reports `TYPE_DECORATOR`, so a
    class-identity MRO walk and an `.impl` unwrap act as fallbacks.
"""

import re
from dataclasses import dataclass, field

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg_dialect

# python_type -> the SQLAlchemy type a reader would assume from that annotation
# alone. When a mapping resolves to exactly this type with no arguments we omit
# it from mapped_column(), matching the SQLAlchemy 2.0 style guide.
_NATURAL_SA_TYPE = {
    "int": "Integer",
    "str": "String",
    "bool": "Boolean",
    "float": "Float",
    "datetime": "DateTime",
    "date": "Date",
    "time": "Time",
}

# Token -> import line. Tokens containing a dot are matched as qualified names.
_PY_IMPORTS = {
    "uuid.UUID": "import uuid",
    "date": "from datetime import date",
    "datetime": "from datetime import datetime",
    "time": "from datetime import time",
    "timedelta": "from datetime import timedelta",
    "Decimal": "from decimal import Decimal",
    "Any": "from typing import Any",
}


@dataclass(frozen=True)
class TypeMapping:
    """Resolved type information for one column."""

    python_type: str
    sa_type: str = ""
    sa_imports: tuple = ()
    py_imports: tuple = ()
    unmapped: bool = False
    raw_db_type: str = ""
    notes: tuple = ()

    @property
    def should_emit_sa_type(self):
        """False when the annotation alone already implies this column type."""
        if not self.sa_type:
            return False
        if "(" in self.sa_type:
            return True
        return _NATURAL_SA_TYPE.get(self.python_type) != self.sa_type


def _uses_token(python_type, token):
    if "." in token:
        return token in python_type
    return re.search(rf"(?<![A-Za-z0-9_.]){re.escape(token)}(?![A-Za-z0-9_])", python_type) is not None


def _with_py_imports(mapping):
    """Derive stdlib/typing imports from python_type when not set explicitly."""
    if mapping.py_imports:
        return mapping
    imports = sorted({line for token, line in _PY_IMPORTS.items() if _uses_token(mapping.python_type, token)})
    return TypeMapping(
        python_type=mapping.python_type,
        sa_type=mapping.sa_type,
        sa_imports=mapping.sa_imports,
        py_imports=tuple(imports),
        unmapped=mapping.unmapped,
        raw_db_type=mapping.raw_db_type,
        notes=mapping.notes,
    )


def _mk(python_type, sa_type, sa_import=(), **kw):
    return _with_py_imports(TypeMapping(python_type, sa_type, tuple(sa_import), **kw))


# --- builders ----------------------------------------------------------------
# Every builder takes (type_instance, MapperContext) and returns a TypeMapping.


def _string(t, ctx):
    length = getattr(t, "length", None)
    return _mk("str", f"String({length})" if length else "String", ("String",))


def _fixed_string(n):
    """Network/identifier types need an explicit length or the annotation hides them."""
    return lambda t, ctx: _mk("str", f"String({n})", ("String",))


def _text(t, ctx):
    return _mk("str", "Text", ("Text",))


def _int(sa_type_name):
    return lambda t, ctx: _mk("int", sa_type_name, (sa_type_name,))


def _numeric(t, ctx):
    precision, scale = getattr(t, "precision", None), getattr(t, "scale", None)
    if precision is not None:
        expr = f"Numeric({precision}, {scale})" if scale else f"Numeric({precision})"
    else:
        expr = "Numeric"
    return _mk("Decimal", expr, ("Numeric",))


def _datetime(t, ctx):
    if getattr(t, "timezone", False):
        return _mk("datetime", "DateTime(timezone=True)", ("DateTime",))
    return _mk("datetime", "DateTime", ("DateTime",))


def _time(t, ctx):
    if getattr(t, "timezone", False):
        return _mk("time", "Time(timezone=True)", ("Time",))
    return _mk("time", "Time", ("Time",))


def _enum_native(t, ctx):
    values = list(getattr(t, "enums", None) or [])
    name = getattr(t, "name", None)
    if values:
        rendered = ", ".join(repr(v) for v in values)
        expr = f"Enum({rendered}, name={name!r})" if name else f"Enum({rendered})"
    else:
        expr = "Enum"
    return _mk("str", expr, ("Enum",))


def _enum_as_string(t, ctx):
    values = list(getattr(t, "enums", None) or [])
    length = max((len(str(v)) for v in values), default=None) or getattr(t, "length", None)
    return _mk("str", f"String({length})" if length else "String", ("String",))


def _enum(t, ctx):
    return _enum_native(t, ctx) if ctx.enum_as_native else _enum_as_string(t, ctx)


def _json(t, ctx):
    return _mk("dict[str, Any]", "JSON", ("JSON",))


def _jsonb(t, ctx):
    if ctx.use_dialect_types:
        return _mk("dict[str, Any]", "JSONB", py_imports=("from typing import Any", "from sqlalchemy.dialects.postgresql import JSONB"))
    return _json(t, ctx)


def _hstore(t, ctx):
    # No generic equivalent: HSTORE values are always text, JSON would widen them.
    return _mk("dict[str, str]", "HSTORE", py_imports=("from sqlalchemy.dialects.postgresql import HSTORE",))


def _array(t, ctx):
    item_type = getattr(t, "item_type", None)
    if item_type is None:
        return _mk("list[Any]", "ARRAY", ("ARRAY",))
    item = map_type(item_type, ctx)
    inner = item.sa_type if item.should_emit_sa_type else (item.sa_type or "String")
    return _mk(f"list[{item.python_type}]", f"ARRAY({inner})", ("ARRAY",))


def _binary(t, ctx):
    return _mk("bytes", "LargeBinary", ("LargeBinary",))


def _unmapped(raw, where):
    return _mk(
        "Any", "",
        unmapped=True,
        raw_db_type=raw,
        notes=(f"No mapping for database type {raw!r} at {where}; emitted as Any. "
               f"Add a rule to generation.custom_type_map or type_overrides.",),
    )


# visit_name (uppercased) -> builder, for types in sqlalchemy.sql.sqltypes.
# Both the Python class name (`BIG_INTEGER`) and the SQL alias (`BIGINT`) forms
# are registered because reflected schemas surface either depending on dialect.
GENERIC_TYPE_MAP = {
    "STRING": _string,
    "VARCHAR": _string,
    "NVARCHAR": _string,
    "CHAR": _string,
    "NCHAR": _string,
    "UNICODE": _string,
    "CLOB": _text,
    "TEXT": _text,
    "UNICODE_TEXT": _text,
    "INTEGER": _int("Integer"),
    "BIG_INTEGER": _int("BigInteger"),
    "SMALL_INTEGER": _int("SmallInteger"),
    "BIGINT": _int("BigInteger"),
    "SMALLINT": _int("SmallInteger"),
    "BOOLEAN": lambda t, c: _mk("bool", "Boolean", ("Boolean",)),
    "BOOL": lambda t, c: _mk("bool", "Boolean", ("Boolean",)),
    "FLOAT": lambda t, c: _mk("float", "Float", ("Float",)),
    "REAL": lambda t, c: _mk("float", "Float", ("Float",)),
    "DOUBLE": lambda t, c: _mk("float", "Double", ("Double",)),
    "DOUBLE_PRECISION": lambda t, c: _mk("float", "Double", ("Double",)),
    "NUMERIC": _numeric,
    "DECIMAL": _numeric,
    "DATE": lambda t, c: _mk("date", "Date", ("Date",)),
    "DATETIME": _datetime,
    "TIMESTAMP": _datetime,
    "TIME": _time,
    # postgresql.INTERVAL reports this visit_name but derives from
    # _AbstractInterval, not sa.Interval, so the class fallback cannot see it.
    "INTERVAL": lambda t, c: _mk("timedelta", "Interval", ("Interval",)),
    "LARGE_BINARY": _binary,
    "BLOB": _binary,
    "BINARY": _binary,
    "VARBINARY": _binary,
    "ENUM": _enum,
    "JSON": _json,
    "UUID": lambda t, c: _mk("uuid.UUID", "Uuid", ("Uuid",)),
    "ARRAY": _array,
}

# (dialect, visit_name) -> builder. Checked before GENERIC_TYPE_MAP.
DIALECT_TYPE_MAP = {
    # --- MySQL / MariaDB ---
    # TINYINT(1) is the universal MySQL boolean idiom; wider TINYINT is an int.
    ("mysql", "TINYINT"): lambda t, c: (
        _mk("bool", "Boolean", ("Boolean",))
        if getattr(t, "display_width", None) == 1
        else _int("SmallInteger")(t, c)
    ),
    ("mysql", "BIT"): lambda t, c: (
        _mk("bool", "Boolean", ("Boolean",))
        if getattr(t, "length", None) == 1
        else _int("BigInteger")(t, c)
    ),
    ("mysql", "MEDIUMINT"): _int("Integer"),
    ("mysql", "YEAR"): _int("SmallInteger"),
    ("mysql", "TINYTEXT"): _text,
    ("mysql", "MEDIUMTEXT"): _text,
    ("mysql", "LONGTEXT"): _text,
    ("mysql", "TINYBLOB"): _binary,
    ("mysql", "MEDIUMBLOB"): _binary,
    ("mysql", "LONGBLOB"): _binary,
    ("mysql", "SET"): _string,
    ("mysql", "DOUBLE"): lambda t, c: _mk("float", "Double", ("Double",)),
    ("mysql", "REAL"): lambda t, c: _mk("float", "Double", ("Double",)),
    # --- PostgreSQL ---
    ("postgresql", "JSONB"): _jsonb,
    ("postgresql", "HSTORE"): _hstore,
    ("postgresql", "BYTEA"): _binary,
    ("postgresql", "MONEY"): lambda t, c: _mk("Decimal", "Numeric", ("Numeric",)),
    # IPv6 + netmask fits in 43 chars; 45 leaves headroom.
    ("postgresql", "INET"): _fixed_string(45),
    ("postgresql", "CIDR"): _fixed_string(45),
    ("postgresql", "MACADDR"): _fixed_string(17),
    ("postgresql", "MACADDR8"): _fixed_string(23),
    ("postgresql", "TSVECTOR"): _text,
    ("postgresql", "TSQUERY"): _text,
    ("postgresql", "XML"): _text,
    ("postgresql", "OID"): _int("BigInteger"),
    # PG bit strings are not integers; keep the raw bits.
    ("postgresql", "BIT"): _binary,
    ("postgresql", "VARBIT"): _binary,
    ("postgresql", "SERIAL"): _int("Integer"),
    ("postgresql", "BIGSERIAL"): _int("BigInteger"),
    ("postgresql", "SMALLSERIAL"): _int("SmallInteger"),
}

# Class-identity fallback for types whose visit_name carries no information
# (every TypeDecorator reports TYPE_DECORATOR). Walked in MRO order so the most
# specific registered class wins.
CLASS_TYPE_MAP = {
    sa.Interval: lambda t, c: _mk("timedelta", "Interval", ("Interval",)),
    pg_dialect.HSTORE: _hstore,
    pg_dialect.JSONB: _jsonb,
    pg_dialect.ARRAY: _array,
    sa.PickleType: lambda t, c: _mk("Any", "PickleType", ("PickleType",)),
}


@dataclass
class MapperContext:
    """Options that influence type resolution for a whole run."""

    dialect: str = ""
    use_dialect_types: bool = False
    enum_as_native: bool = False
    # {visit_name_upper: {"python_type": ..., "sa_type": ...}}
    custom_type_map: dict = field(default_factory=dict)
    # {"table.column": {"python_type": ..., "sa_type": ...}}
    column_overrides: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)


def _dialect_of(sa_type):
    parts = type(sa_type).__module__.split(".")
    if len(parts) >= 3 and parts[1] == "dialects":
        return parts[2]
    return ""


def _visit_name(sa_type):
    return str(getattr(sa_type, "__visit_name__", "") or "").upper()


def _apply_override(override, raw):
    """Build a TypeMapping from a user-supplied {python_type, sa_type} dict."""
    python_type = override.get("python_type", "Any")
    sa_type = override.get("sa_type", "")
    sa_imports = tuple(override.get("sa_imports") or ((sa_type.split("(")[0],) if sa_type else ()))
    py_imports = tuple(override.get("py_imports") or ())
    dialect_import = override.get("dialect_import")
    if dialect_import:
        py_imports = py_imports + (dialect_import,)
    mapping = TypeMapping(python_type, sa_type, sa_imports, py_imports, raw_db_type=raw)
    return mapping if py_imports else _with_py_imports(mapping)


def map_type(sa_type, ctx=None, table=None, column=None):
    """Resolve a reflected SQLAlchemy type instance to a TypeMapping."""
    ctx = ctx or MapperContext()
    where = f"{table or '?'}.{column or '?'}"

    # DOMAIN types wrap the real type; unwrap before anything else.
    if _visit_name(sa_type) == "DOMAIN":
        impl = getattr(sa_type, "impl", None)
        if impl is not None:
            sa_type = impl() if callable(impl) else impl

    raw = _visit_name(sa_type) or type(sa_type).__name__

    if table and column:
        override = ctx.column_overrides.get(f"{table}.{column}")
        if override:
            return _apply_override(override, raw)
    if raw in ctx.custom_type_map:
        return _apply_override(ctx.custom_type_map[raw], raw)

    # TYPE_DECORATOR tells us nothing about the actual type, so skip the
    # visit_name tables entirely and resolve by class identity / impl.
    if raw != "TYPE_DECORATOR":
        dialect = _dialect_of(sa_type)
        builder = DIALECT_TYPE_MAP.get((dialect, raw)) or GENERIC_TYPE_MAP.get(raw)
        if builder is not None:
            return builder(sa_type, ctx)

    for cls in type(sa_type).__mro__:
        builder = CLASS_TYPE_MAP.get(cls)
        if builder is not None:
            return builder(sa_type, ctx)

    impl = getattr(sa_type, "impl", None)
    if impl is not None:
        try:
            inner = impl() if callable(impl) else impl
            if inner is not sa_type:
                return map_type(inner, ctx, table, column)
        except Exception:
            pass

    ctx.warnings.append(f"Unmapped database type {raw!r} on {where}")
    return _unmapped(raw, where)


def collect_imports(mappings):
    """Split a set of mappings into sqlalchemy names, dialect lines, and stdlib lines."""
    plain, dialect_lines, py_lines = set(), set(), set()
    for mapping in mappings:
        plain.update(mapping.sa_imports)
        for line in mapping.py_imports:
            (dialect_lines if line.startswith("from sqlalchemy.dialects.") else py_lines).add(line)
    return sorted(plain), sorted(dialect_lines), sorted(py_lines)


def describe_builtin_mappings():
    """JSON-serialisable summary of built-in rules, for the IDE settings UI."""
    entries = [{"dialect": "*", "db_type": name} for name in sorted(GENERIC_TYPE_MAP)]
    entries += [{"dialect": d, "db_type": n} for d, n in sorted(DIALECT_TYPE_MAP)]
    return entries
