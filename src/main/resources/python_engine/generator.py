#!/usr/bin/env python3
"""SQLAlchemy model generator -- CLI entry point.

Protocol contract with the IDE plugin:
  * input   : a JSON config file passed via --config
  * output  : exactly one JSON object on stdout (or written to --result-file)
  * logging : stderr only, never stdout
  * exit    : see engine.protocol.EXIT_* constants

stdout is emitted ASCII-escaped by default so a charset mismatch on the reading
side can never corrupt non-ASCII identifiers or comments. Use --pretty for
human-readable output when running the engine by hand.
"""

import argparse
import io
import json
import logging
import os
import sys
import traceback

# Make this script's directory importable regardless of the caller's CWD.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import dependencies  # noqa: E402
from engine.protocol import (  # noqa: E402
    ACTION_GENERATE,
    ACTION_LIST_TABLES,
    ACTION_TEST_CONNECTION,
    Config,
    Result,
    ERR_CONNECTION_FAILED,
    ERR_GENERATION_FAILED,
    ERR_INTERNAL,
    ERR_INVALID_CONFIG,
    ERR_MISSING_DEPENDENCIES,
)

log = logging.getLogger("sqlalchemy_generator")


def _force_utf8_streams():
    """Windows consoles/pipes may default to a legacy codepage."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
        elif hasattr(stream, "buffer"):
            wrapper = io.TextIOWrapper(stream.buffer, encoding="utf-8", errors="replace")
            if stream is sys.stdout:
                sys.stdout = wrapper
            else:
                sys.stderr = wrapper


def _emit(result, result_file=None, pretty=False):
    """Write the single JSON result object. Returns the process exit code."""
    payload = json.dumps(
        result.to_dict(),
        ensure_ascii=not pretty,
        indent=2 if pretty else None,
        sort_keys=False,
    )
    try:
        if result_file:
            with open(result_file, "w", encoding="utf-8") as handle:
                handle.write(payload)
        else:
            sys.stdout.write(payload)
            sys.stdout.write("\n")
            sys.stdout.flush()
    except OSError as exc:  # pragma: no cover - disk/pipe failure
        sys.stderr.write(f"Failed to emit result: {exc}\n")
        return 1
    return result.exit_code


def _configure_logging(verbose):
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if verbose else logging.INFO,
        format="[sqlalchemy-generator] %(levelname)s %(name)s: %(message)s",
    )


def _load_config(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _mapper_context(config):
    from engine.type_mapper import MapperContext

    return MapperContext(
        dialect=config.connection.dialect,
        use_dialect_types=config.generation.use_dialect_types,
        enum_as_native=config.generation.enum_as_native,
        custom_type_map=dict(config.generation.custom_type_map),
        column_overrides=dict(config.generation.type_overrides),
    )


def _run_test_connection(config):
    from engine.inspector import test_connection

    details = test_connection(config.connection)
    return Result.success(
        ACTION_TEST_CONNECTION,
        message=f"Connected to {details['database']} ({details['dialect']} {details['server_version']})",
        details=details,
    )


def _run_list_tables(config):
    from engine.inspector import list_tables, load_tables
    from engine.schema import tables_to_summary

    if config.generation.mock_metadata:
        warnings = []
        ctx = _mapper_context(config)
        tables = load_tables(config, ctx, warnings)
        return Result.success(
            ACTION_LIST_TABLES,
            message=f"{len(tables)} table(s)",
            tables=tables_to_summary(tables),
            warnings=warnings,
        )

    entries = list_tables(config.connection, include_views=config.generation.include_views)
    return Result.success(
        ACTION_LIST_TABLES,
        message=f"{len(entries)} table(s)",
        tables=entries,
    )


def _run_generate(config):
    from engine.inspector import load_tables
    from engine.relationships import infer_relationships
    from engine.renderer import Renderer

    warnings = []
    ctx = _mapper_context(config)
    tables = load_tables(config, ctx, warnings)
    if not tables:
        raise LookupError(
            "No tables were resolved. Check that generation.tables matches the database "
            "(or that generation.mock_metadata contains those keys)."
        )

    if config.generation.generate_relationships:
        infer_relationships(tables, prefixes=config.generation.table_prefix_strip)

    warnings.extend(ctx.warnings)
    renderer = Renderer(config.generation, template_dir=config.template_dir or None)
    files = renderer.render(tables)

    total_lines = sum(f["content"].count("\n") + 1 for f in files)
    return Result.success(
        ACTION_GENERATE,
        message=f"Generated {len(files)} file(s) for {len(tables)} table(s), {total_lines} lines",
        files=files,
        warnings=warnings,
        details={
            "mode": config.generation.mode,
            "output_dir": config.generation.output_dir,
            "table_count": len(tables),
            "unmapped_columns": [
                f"{t.name}.{c.name}" for t in tables for c in t.columns if c.unmapped
            ],
        },
    )


_DISPATCH = {
    ACTION_TEST_CONNECTION: _run_test_connection,
    ACTION_LIST_TABLES: _run_list_tables,
    ACTION_GENERATE: _run_generate,
}


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="generator.py",
        description="Generate SQLAlchemy 2.0 ORM models from a database schema.",
    )
    parser.add_argument("--config", required=False, help="Path to the JSON config file.")
    parser.add_argument("--result-file", default=None,
                        help="Write the JSON result here instead of stdout (avoids pipe limits).")
    parser.add_argument("--pretty", action="store_true",
                        help="Human-readable JSON with UTF-8 literals instead of ASCII escapes.")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging to stderr.")
    parser.add_argument("--version", action="store_true", help="Print engine version and exit.")
    parser.add_argument("--describe-types", action="store_true",
                        help="Emit the built-in type mapping table and exit.")
    args = parser.parse_args(argv)

    _force_utf8_streams()
    _configure_logging(args.verbose)

    if args.version:
        from engine import __version__
        return _emit(Result.success("", details={"version": __version__}), args.result_file, args.pretty)

    if args.describe_types:
        from engine.type_mapper import describe_builtin_mappings
        return _emit(
            Result.success("", details={"mappings": describe_builtin_mappings()}),
            args.result_file, args.pretty,
        )

    if not args.config:
        return _emit(
            Result.error("", ERR_INVALID_CONFIG, "--config is required"),
            args.result_file, args.pretty,
        )

    # --- load config (no heavy imports yet) ---
    try:
        raw = _load_config(args.config)
    except FileNotFoundError:
        return _emit(Result.error("", ERR_INVALID_CONFIG, f"Config file not found: {args.config}"),
                     args.result_file, args.pretty)
    except json.JSONDecodeError as exc:
        return _emit(Result.error("", ERR_INVALID_CONFIG, f"Config is not valid JSON: {exc}"),
                     args.result_file, args.pretty)
    except OSError as exc:
        return _emit(Result.error("", ERR_INVALID_CONFIG, f"Cannot read config: {exc}"),
                     args.result_file, args.pretty)

    try:
        config = Config.from_dict(raw)
    except ValueError as exc:
        return _emit(Result.error("", ERR_INVALID_CONFIG, str(exc)), args.result_file, args.pretty)

    problems = config.validate()
    if problems:
        return _emit(
            Result.error(config.action, ERR_INVALID_CONFIG, "; ".join(problems),
                         details={"problems": problems}),
            args.result_file, args.pretty,
        )

    # --- dependency gate: runs before sqlalchemy is imported anywhere ---
    needs_driver = not config.generation.mock_metadata
    dep_check = dependencies.check(config.connection.dialect if needs_driver else None)
    if not dep_check["ok"]:
        return _emit(
            Result.error(
                config.action,
                ERR_MISSING_DEPENDENCIES,
                "The Python interpreter is missing required packages.",
                details=dep_check,
            ),
            args.result_file, args.pretty,
        )

    # --- dispatch ---
    from engine.inspector import DatabaseError

    try:
        result = _DISPATCH[config.action](config)
    except DatabaseError as exc:
        log.debug("Connection failure\n%s", traceback.format_exc())
        result = Result.error(config.action, ERR_CONNECTION_FAILED, str(exc),
                              details={"connection": config.connection.to_dict(redact=True)})
    except LookupError as exc:
        result = Result.error(config.action, ERR_GENERATION_FAILED, str(exc))
    except Exception as exc:  # noqa: BLE001 - top-level guard for the IPC contract
        log.error("Unhandled failure: %s\n%s", exc, traceback.format_exc())
        result = Result.error(config.action, ERR_INTERNAL, f"{type(exc).__name__}: {exc}")

    return _emit(result, args.result_file, args.pretty)


if __name__ == "__main__":
    sys.exit(main())
