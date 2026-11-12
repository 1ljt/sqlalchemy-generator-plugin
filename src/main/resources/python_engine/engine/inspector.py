"""Database access: reflection over a live connection, or offline from JSON.

Both paths produce the same `TableMeta` list so downstream rendering never
needs to know which one ran.
"""

import logging
import sys

from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.pool import NullPool

from .schema import build_table_meta
from .sql_types import UnknownSqlType, type_from_string

log = logging.getLogger("sqlalchemy_generator.inspector")


class DatabaseError(RuntimeError):
    """Connection or reflection failure, with a message safe to show the user."""


def create_engine_for(connection):
    """Build a short-lived engine (NullPool: the process exits right after)."""
    try:
        url = connection.build_url()
    except ValueError as exc:
        raise DatabaseError(str(exc)) from exc
    log.debug("Connecting to %s", connection.to_dict(redact=True))
    try:
        return create_engine(url, connect_args=connection.connect_args(), poolclass=NullPool)
    except ImportError as exc:
        # Raised when the DBAPI driver is absent or broken. On Windows a missing
        # libpq DLL surfaces here too. Not a SQLAlchemyError, so it needs its own
        # branch or it would escape as an opaque internal_error.
        driver = "psycopg2" if connection.dialect == "postgresql" else "pymysql"
        raise DatabaseError(
            f"The Python driver for {connection.dialect} could not be loaded ({exc}). "
            f'Install it with: "{sys.executable}" -m pip install {driver}'
        ) from exc
    except SQLAlchemyError as exc:
        raise DatabaseError(_readable(exc)) from exc


def _readable(exc):
    """Strip SQLAlchemy's stack framing down to the driver's own message."""
    text = str(getattr(exc, "orig", exc) or exc).strip()
    return text.splitlines()[0] if text else exc.__class__.__name__


def test_connection(connection):
    """Verify reachability. Returns dialect/server details for the UI."""
    engine = create_engine_for(connection)
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
            dialect = conn.engine.dialect
            version = getattr(dialect, "server_version_info", None)
            return {
                "connected": True,
                "dialect": dialect.name,
                "driver": getattr(dialect, "driver", ""),
                "server_version": ".".join(str(p) for p in version) if version else "",
                "database": connection.database,
            }
    except OperationalError as exc:
        raise DatabaseError(_readable(exc)) from exc
    except SQLAlchemyError as exc:
        raise DatabaseError(_readable(exc)) from exc
    finally:
        engine.dispose()


def list_tables_from_inspector(inspector, schema=None, include_views=False):
    """Enumerate tables (and optionally views) using an existing inspector.

    Split out from `list_tables` so the enumeration logic can be exercised
    against any SQLAlchemy inspector -- tests drive it with in-memory SQLite.
    """
    names = list(inspector.get_table_names(schema=schema))
    kinds = {name: "table" for name in names}
    if include_views:
        for view in _safe(inspector.get_view_names, schema=schema) or []:
            if view not in kinds:
                names.append(view)
                kinds[view] = "view"
    return [
        {"name": name, "kind": kinds[name], "schema": schema,
         "comment": _safe_table_comment(inspector, name, schema)}
        for name in sorted(names)
    ]


def list_tables(connection, include_views=False):
    """All user tables (and optionally views), sorted by name."""
    engine = create_engine_for(connection)
    try:
        return list_tables_from_inspector(
            inspect(engine), connection.schema or None, include_views
        )
    except OperationalError as exc:
        raise DatabaseError(_readable(exc)) from exc
    except SQLAlchemyError as exc:
        raise DatabaseError(_readable(exc)) from exc
    finally:
        engine.dispose()


def _safe_table_comment(inspector, name, schema):
    # Not every dialect/driver implements table comments.
    try:
        info = inspector.get_table_comment(name, schema=schema)
        return (info or {}).get("text")
    except (NotImplementedError, SQLAlchemyError):
        return None


def reflect_tables_from_inspector(inspector, table_names, schema, ctx, generation):
    """Reflect the given tables into TableMeta objects using an existing inspector."""
    return [_reflect_one(inspector, name, schema, ctx, generation) for name in table_names]


def inspect_tables(connection, table_names, ctx, generation):
    """Reflect the selected tables into TableMeta objects."""
    engine = create_engine_for(connection)
    try:
        return reflect_tables_from_inspector(
            inspect(engine), table_names, connection.schema or None, ctx, generation
        )
    except OperationalError as exc:
        raise DatabaseError(_readable(exc)) from exc
    except SQLAlchemyError as exc:
        raise DatabaseError(_readable(exc)) from exc
    finally:
        engine.dispose()


def _reflect_one(inspector, name, schema, ctx, generation):
    columns = inspector.get_columns(name, schema=schema)
    pk = _safe(inspector.get_pk_constraint, name, schema) or {}
    fks = _safe(inspector.get_foreign_keys, name, schema) or []
    indexes = _safe(inspector.get_indexes, name, schema) or []
    uniques = _safe(inspector.get_unique_constraints, name, schema) or []
    comment = _safe_table_comment(inspector, name, schema)
    return _build(name, columns, pk, fks, indexes, uniques, comment, schema, ctx, generation)


def _safe(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except (NotImplementedError, SQLAlchemyError):
        return None


def tables_from_mock(mock_metadata, generation, ctx, warnings=None):
    """Build TableMeta from JSON reflection dicts. No database involved."""
    warnings = warnings if warnings is not None else []
    dialect = getattr(ctx, "dialect", "") or ""
    tables = []
    for name in generation.tables:
        spec = mock_metadata.get(name)
        if spec is None:
            warnings.append(f"mock_metadata has no entry for table {name!r}; skipped")
            continue
        columns = []
        for col in spec.get("columns") or []:
            entry = dict(col)
            db_type = entry.pop("db_type", None)
            if db_type is not None and "type" not in entry:
                try:
                    entry["type"] = type_from_string(db_type, dialect)
                except UnknownSqlType as exc:
                    warnings.append(f"{name}.{entry.get('name')}: {exc}")
                    entry["type"] = None
            columns.append(entry)

        pk_names = spec.get("primary_keys") or spec.get("primary_key") or []
        if isinstance(pk_names, dict):
            pk_names = pk_names.get("constrained_columns") or []
        tables.append(
            _build(
                name,
                columns,
                {"constrained_columns": list(pk_names)},
                spec.get("foreign_keys") or [],
                spec.get("indexes") or [],
                spec.get("unique_constraints") or [],
                spec.get("comment"),
                spec.get("schema"),
                ctx,
                generation,
            )
        )
    return tables


def _build(name, columns, pk, fks, indexes, uniques, comment, schema, ctx, generation):
    return build_table_meta(
        table_name=name,
        columns=columns,
        primary_key=pk,
        foreign_keys=fks,
        indexes=indexes,
        unique_constraints=uniques,
        comment=comment,
        schema=schema,
        ctx=ctx,
        prefixes=generation.table_prefix_strip,
        escape_builtins=generation.escape_builtin_names,
        include_comments=generation.include_docstrings or generation.include_column_comments,
        class_name_override=generation.class_name_overrides.get(name),
        singularize_tables=generation.singularize_class_names,
    )


def load_tables(config, ctx, warnings):
    """Dispatch to the offline or live path according to the config."""
    generation = config.generation
    if generation.mock_metadata:
        return tables_from_mock(generation.mock_metadata, generation, ctx, warnings)
    return inspect_tables(config.connection, generation.tables, ctx, generation)
