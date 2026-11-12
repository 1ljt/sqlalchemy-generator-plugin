# SQLAlchemy Generation Engine

The Python half of the plugin. Stateless and CLI-driven: it reads a JSON config,
talks to the database through SQLAlchemy, and returns generated source as JSON.
The Kotlin side owns all IDE interaction and all file writing.

## Layout

```
python_engine/
├── generator.py          CLI entry point (the only file the plugin invokes)
├── engine/
│   ├── dependencies.py   Runtime dependency detection -> structured error
│   ├── protocol.py       Config / Result JSON contract, exit codes
│   ├── sql_types.py      SQL type string -> SQLAlchemy type (offline path)
│   ├── type_mapper.py    DB type -> Python annotation + SQLAlchemy type
│   ├── naming.py         Identifier conversion, pluralisation, keyword escaping
│   ├── schema.py         Reflection dicts -> TableMeta, mapped_column() rendering
│   ├── relationships.py  Foreign keys -> relationship(), incl. self-references
│   ├── inspector.py      Live reflection, or offline from mock metadata
│   └── renderer.py       Jinja2 context assembly + module rendering
├── templates/            Default Jinja2 templates (user-overridable)
├── tools/                Development helpers (not shipped in the plugin jar)
└── tests/                unittest suite; also runs under pytest
```

## Requirements

```
sqlalchemy>=2.0.0
jinja2>=3.0.0
pymysql>=1.0.0           # MySQL / MariaDB
psycopg2-binary>=2.9.0   # PostgreSQL
```

`psycopg2-binary` (not `psycopg2`) — the plain package needs a compiler and
libpq headers, which a one-click install cannot assume.

## CLI

```
python generator.py --config <path>            # JSON result on stdout
python generator.py --config <path> --result-file <path>   # result to a file
python generator.py --config <path> --pretty   # human-readable, UTF-8 literals
python generator.py --version
python generator.py --describe-types           # built-in type mapping table
```

`--result-file` exists because very large schemas can exceed OS pipe buffers;
prefer it over reading stdout for anything but small results.

### Protocol guarantees

- **stdout** carries exactly one JSON object and nothing else.
- **stderr** carries all logging. Never parse it as JSON.
- By default the JSON is ASCII-escaped, so a charset mismatch on the reading side
  cannot corrupt non-ASCII table or column comments. Use `--pretty` for humans.

### Exit codes

| Code | Meaning | `error_type` |
|---|---|---|
| 0 | success | — |
| 1 | unexpected internal failure | `internal_error` |
| 2 | Python packages missing | `missing_dependencies` |
| 3 | cannot reach the database | `connection_failed` |
| 4 | config invalid or incomplete | `invalid_config` |
| 5 | generation failed | `generation_failed` |

### Missing dependencies

`error_type: "missing_dependencies"` carries everything the IDE needs to render
an actionable dialog:

```json
{
  "status": "error",
  "error_type": "missing_dependencies",
  "message": "The Python interpreter is missing required packages.",
  "details": {
    "ok": false,
    "missing": ["psycopg2"],
    "missing_distributions": ["psycopg2-binary"],
    "install_command": "\"C:\\Python313\\python.exe\" -m pip install psycopg2-binary",
    "interpreter": "C:\\Python313\\python.exe",
    "python_version": "3.13.5"
  }
}
```

The engine never installs anything itself.

## Config shape

```jsonc
{
  "action": "generate",            // generate | list_tables | test_connection
  "connection": {
    "dialect": "mysql",            // mysql | mariadb | postgresql
    "host": "localhost",
    "port": 3306,                  // omitted -> dialect default
    "database": "shop",
    "user": "root",
    "password": "secret",          // never echoed back; results redact it
    "charset": "utf8mb4",
    "schema": null,                // PostgreSQL schema
    "extra_params": {}             // scalars -> URL query; dicts -> connect_args
  },
  "generation": {
    "mode": "multiple",            // multiple | single
    "tables": ["users", "orders"],
    "output_dir": "/abs/path",     // echoed back; the engine never writes files
    "single_file_name": "models.py",
    "base_class_name": "Base",
    "base_file_name": "base.py",
    "base_import": "",             // e.g. "from myapp.db import Base" -> no base.py
    "generate_init": true,
    "generate_relationships": true,
    "table_prefix_strip": ["t_", "tbl_"],
    "singularize_class_names": true,
    "include_docstrings": true,
    "include_column_comments": true,
    "use_future_annotations": true,
    "use_dialect_types": false,    // true -> JSONB/HSTORE instead of JSON
    "enum_as_native": false,       // true -> Enum('a','b') instead of String(n)
    "escape_builtin_names": false, // true -> id_ instead of id
    "class_name_overrides": {},    // {"users": "Account"}
    "type_overrides": {},          // {"users.status": {"python_type": "str", "sa_type": "String(20)"}}
    "custom_type_map": {},         // {"JSONB": {...}} keyed by DB type name
    "mock_metadata": {},           // offline mode; see below
    "include_views": false
  },
  "template_dir": null             // null -> bundled templates
}
```

### Result shape

```json
{
  "status": "success",
  "action": "generate",
  "message": "Generated 8 file(s) for 6 table(s), 184 lines",
  "files": [
    {"relative_path": "users.py", "content": "...", "kind": "model", "table_names": ["users"]}
  ],
  "tables": [],
  "warnings": [],
  "details": {"mode": "multiple", "table_count": 6, "unmapped_columns": []}
}
```

`relative_path` is relative to `generation.output_dir`. The engine returns
content; **the Kotlin side writes the files** (inside `WriteCommandAction`, then
`VfsUtil.markDirtyAndRefresh`), so the IDE's undo stack and VFS stay consistent.

### Offline mode

Setting `generation.mock_metadata` skips the database entirely, which is how the
test suite runs without a server:

```json
{"users": {"primary_keys": ["id"],
           "columns": [{"name": "id", "db_type": "INTEGER", "nullable": false}]}}
```

`db_type` is a SQL type string (`"VARCHAR(50)"`, `"DECIMAL(10,2)"`,
`"postgresql.JSONB"`), parsed by `engine/sql_types.py`.

## Running the tests

No third-party test runner required:

```bash
cd src/main/resources/python_engine
python -m unittest discover -s tests -t .
python tools/measure_coverage.py     # stdlib-only line coverage
```

`pytest` also collects these tests unchanged if you prefer it.

The suite includes end-to-end tests that spawn `generator.py` as a subprocess,
then write the generated files, import them, and verify SQLAlchemy configures the
mappers and can round-trip rows through SQLite. That is what catches mistakes
`ast.parse()` cannot — for example `mapped_column(String(50), 'userName')` is
valid syntax but raises `ArgumentError` at class-definition time, because the
column name must precede the type.

## Notes for the Kotlin side

- Pass `PYTHONUTF8=1` and `PYTHONIOENCODING=utf-8` in the process environment.
  Without them a Windows child process falls back to the console codepage and
  CJK comments cannot be decoded.
- Write the config to a temp file and pass `--config`; never put the password on
  the command line, where it is visible in the process list.
- Delete the temp config as soon as the process exits — it contains a password.
- Switch on the exit code, then read `error_type` for the message to show.
