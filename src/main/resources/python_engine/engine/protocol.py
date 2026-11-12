"""JSON wire protocol between the IDE plugin (Kotlin) and this engine.

Contract, enforced everywhere in this package:
  * stdin/config file -> a Config object (unknown keys ignored, for forward compat)
  * stdout            -> exactly one JSON object (a Result), nothing else
  * stderr            -> free-form logging
"""

from dataclasses import dataclass, field, asdict

# --- error classification ---------------------------------------------------
ERR_MISSING_DEPENDENCIES = "missing_dependencies"
ERR_CONNECTION_FAILED = "connection_failed"
ERR_INVALID_CONFIG = "invalid_config"
ERR_GENERATION_FAILED = "generation_failed"
ERR_INTERNAL = "internal_error"

# Exit codes the Kotlin side switches on.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_MISSING_DEPS = 2
EXIT_CONNECTION_FAILED = 3
EXIT_INVALID_CONFIG = 4
EXIT_GENERATION_FAILED = 5

EXIT_CODE_FOR_ERROR = {
    ERR_MISSING_DEPENDENCIES: EXIT_MISSING_DEPS,
    ERR_CONNECTION_FAILED: EXIT_CONNECTION_FAILED,
    ERR_INVALID_CONFIG: EXIT_INVALID_CONFIG,
    ERR_GENERATION_FAILED: EXIT_GENERATION_FAILED,
    ERR_INTERNAL: EXIT_ERROR,
}

DIALECT_MYSQL = "mysql"
DIALECT_POSTGRESQL = "postgresql"
SUPPORTED_DIALECTS = (DIALECT_MYSQL, DIALECT_POSTGRESQL, "mariadb")

MODE_SINGLE = "single"
MODE_MULTIPLE = "multiple"

DEFAULT_PORTS = {DIALECT_MYSQL: 3306, "mariadb": 3306, DIALECT_POSTGRESQL: 5432}

ACTION_TEST_CONNECTION = "test_connection"
ACTION_LIST_TABLES = "list_tables"
ACTION_GENERATE = "generate"
VALID_ACTIONS = (ACTION_TEST_CONNECTION, ACTION_LIST_TABLES, ACTION_GENERATE)


def _pick(mapping, key, default):
    """mapping.get(key) falling back to `default` when absent or explicitly None."""
    if not isinstance(mapping, dict):
        return default
    value = mapping.get(key, default)
    return default if value is None else value


@dataclass
class ConnectionConfig:
    """Database coordinates. Never logged in full -- `password` is redacted."""

    dialect: str = DIALECT_MYSQL
    host: str = "localhost"
    port: int = 0
    database: str = ""
    user: str = ""
    password: str = ""
    schema: str = ""
    charset: str = ""
    # Extra driver kwargs, e.g. {"ssl": {"ca": "/path/ca.pem"}}
    extra_params: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data):
        data = data if isinstance(data, dict) else {}
        dialect = str(_pick(data, "dialect", DIALECT_MYSQL)).lower()
        port = _pick(data, "port", 0)
        try:
            port = int(port)
        except (TypeError, ValueError):
            port = 0
        if not port:
            port = DEFAULT_PORTS.get(dialect, 0)
        extra = _pick(data, "extra_params", {})
        return cls(
            dialect=dialect,
            host=str(_pick(data, "host", "localhost")),
            port=port,
            database=str(_pick(data, "database", "")),
            user=str(_pick(data, "user", "")),
            password=str(_pick(data, "password", "")),
            schema=str(_pick(data, "schema", "")),
            charset=str(_pick(data, "charset", "")),
            extra_params=extra if isinstance(extra, dict) else {},
        )

    def to_dict(self, redact=True):
        data = asdict(self)
        if redact and data.get("password"):
            data["password"] = "***"
        return data

    def build_url(self):
        """Assemble a SQLAlchemy URL. Password is URL-escaped."""
        from urllib.parse import quote_plus

        driver = {
            DIALECT_MYSQL: "mysql+pymysql",
            "mariadb": "mariadb+pymysql",
            DIALECT_POSTGRESQL: "postgresql+psycopg2",
        }.get(self.dialect)
        if driver is None:
            raise ValueError(
                f"Unsupported dialect {self.dialect!r}. Expected one of {list(SUPPORTED_DIALECTS)}."
            )

        credentials = quote_plus(self.user)
        if self.password:
            credentials += f":{quote_plus(self.password)}"

        url = f"{driver}://{credentials}@{self.host}:{self.port}/{quote_plus(self.database)}"

        query = {}
        if self.charset:
            query["charset"] = self.charset
        for key, value in self.extra_params.items():
            # Nested dicts (e.g. ssl options) are not expressible in a URL query
            # string; callers needing them should pass connect_args instead.
            if isinstance(value, (str, int, float, bool)):
                query[str(key)] = str(value)
        if query:
            url += "?" + "&".join(f"{k}={quote_plus(v)}" for k, v in sorted(query.items()))
        return url

    def connect_args(self):
        """Driver kwargs that cannot live in the URL."""
        args = {}
        for key, value in self.extra_params.items():
            if isinstance(value, dict):
                args[key] = value
        return args


@dataclass
class GenerationConfig:
    """Everything that shapes the emitted Python source."""

    mode: str = MODE_MULTIPLE
    output_dir: str = ""
    single_file_name: str = "models.py"
    base_class_name: str = "Base"
    base_file_name: str = "base.py"
    generate_base_file: bool = True
    generate_init: bool = True
    init_file_name: str = "__init__.py"
    table_prefix_strip: list = field(default_factory=list)
    generate_relationships: bool = True
    include_docstrings: bool = True
    include_column_comments: bool = True
    use_future_annotations: bool = True
    use_dialect_types: bool = False
    enum_as_native: bool = False
    escape_builtin_names: bool = False
    # `users` -> class `User`. Turn off to keep the table name verbatim (`Users`).
    singularize_class_names: bool = True
    # {visit_name_upper: {"python_type": ..., "sa_type": ...}}
    custom_type_map: dict = field(default_factory=dict)
    # {"table.column": {"python_type": ..., "sa_type": ...}}
    type_overrides: dict = field(default_factory=dict)
    tables: list = field(default_factory=list)
    include_views: bool = False
    # Explicit class-name overrides: {table_name: ClassName}
    class_name_overrides: dict = field(default_factory=dict)
    # Import line for a user-supplied declarative base, e.g.
    # "from myapp.db import Base". When set, no base.py is generated.
    base_import: str = ""
    # Offline metadata: {table_name: {columns:[{name, db_type, nullable, ...}], ...}}
    # When non-empty, no database connection is made. Used by tests and by the
    # plugin's "preview without connecting" path.
    mock_metadata: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data):
        data = data if isinstance(data, dict) else {}

        def _list(key, default=()):
            value = _pick(data, key, None)
            if value is None:
                return list(default)
            if isinstance(value, str):
                return [value] if value else []
            if isinstance(value, (list, tuple)):
                return [str(v) for v in value]
            return list(default)

        def _dict(key):
            value = _pick(data, key, {})
            return value if isinstance(value, dict) else {}

        mode = str(_pick(data, "mode", MODE_MULTIPLE)).lower()
        if mode not in (MODE_SINGLE, MODE_MULTIPLE):
            mode = MODE_MULTIPLE

        base_import = str(_pick(data, "base_import", "") or "")
        generate_base = bool(_pick(data, "generate_base_file", True))
        # A user-supplied base import supersedes generating our own base module.
        if base_import:
            generate_base = False

        return cls(
            mode=mode,
            output_dir=str(_pick(data, "output_dir", "")),
            single_file_name=str(_pick(data, "single_file_name", "models.py")),
            base_class_name=str(_pick(data, "base_class_name", "Base")),
            base_file_name=str(_pick(data, "base_file_name", "base.py")),
            generate_base_file=generate_base,
            generate_init=bool(_pick(data, "generate_init", True)),
            init_file_name=str(_pick(data, "init_file_name", "__init__.py")),
            table_prefix_strip=_list("table_prefix_strip"),
            generate_relationships=bool(_pick(data, "generate_relationships", True)),
            include_docstrings=bool(_pick(data, "include_docstrings", True)),
            include_column_comments=bool(_pick(data, "include_column_comments", True)),
            use_future_annotations=bool(_pick(data, "use_future_annotations", True)),
            use_dialect_types=bool(_pick(data, "use_dialect_types", False)),
            enum_as_native=bool(_pick(data, "enum_as_native", False)),
            escape_builtin_names=bool(_pick(data, "escape_builtin_names", False)),
            singularize_class_names=bool(_pick(data, "singularize_class_names", True)),
            custom_type_map=_dict("custom_type_map"),
            type_overrides=_dict("type_overrides"),
            tables=_list("tables"),
            include_views=bool(_pick(data, "include_views", False)),
            class_name_overrides=_dict("class_name_overrides"),
            base_import=base_import,
            mock_metadata=_dict("mock_metadata"),
        )


@dataclass
class Config:
    """Top-level request document."""

    action: str = ACTION_GENERATE
    connection: ConnectionConfig = field(default_factory=ConnectionConfig)
    generation: GenerationConfig = field(default_factory=GenerationConfig)
    template_dir: str = ""

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise ValueError("config must be a JSON object")
        action = str(_pick(data, "action", ACTION_GENERATE)).lower()
        if action not in VALID_ACTIONS:
            raise ValueError(f"Unknown action {action!r}. Expected one of {list(VALID_ACTIONS)}.")
        return cls(
            action=action,
            connection=ConnectionConfig.from_dict(data.get("connection")),
            generation=GenerationConfig.from_dict(data.get("generation")),
            template_dir=str(_pick(data, "template_dir", "") or ""),
        )

    def validate(self):
        """Return a list of human-readable problems; empty means the config is usable."""
        problems = []
        offline = bool(self.generation.mock_metadata)
        if not offline:
            if not self.connection.database:
                problems.append("connection.database is required")
            if not self.connection.host:
                problems.append("connection.host is required")
            if self.connection.dialect not in SUPPORTED_DIALECTS:
                problems.append(
                    f"connection.dialect {self.connection.dialect!r} is not supported "
                    f"(expected one of {list(SUPPORTED_DIALECTS)})"
                )
        if self.action == ACTION_GENERATE and not self.generation.tables:
            problems.append("generation.tables must list at least one table")
        return problems


@dataclass
class GeneratedFile:
    """One output file. The Kotlin side resolves `relative_path` against output_dir."""

    relative_path: str
    content: str
    kind: str = "model"
    table_names: list = field(default_factory=list)

    def to_dict(self):
        return {
            "relative_path": self.relative_path,
            "content": self.content,
            "kind": self.kind,
            "table_names": list(self.table_names),
        }


@dataclass
class Result:
    """The single JSON object written to stdout."""

    status: str = "success"
    action: str = ""
    message: str = ""
    error_type: str = ""
    files: list = field(default_factory=list)
    tables: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    details: dict = field(default_factory=dict)

    @staticmethod
    def success(action, message="", files=(), tables=(), warnings=(), details=None):
        return Result(
            status="success",
            action=action,
            message=message,
            files=[f.to_dict() if hasattr(f, "to_dict") else f for f in files],
            tables=list(tables),
            warnings=list(warnings),
            details=details or {},
        )

    @staticmethod
    def error(action, error_type, message, details=None, warnings=()):
        return Result(
            status="error",
            action=action,
            message=message,
            error_type=error_type,
            warnings=list(warnings),
            details=details or {},
        )

    @property
    def exit_code(self):
        if self.status == "success":
            return EXIT_OK
        return EXIT_CODE_FOR_ERROR.get(self.error_type, EXIT_ERROR)

    def to_dict(self):
        data = {
            "status": self.status,
            "action": self.action,
            "message": self.message,
            "files": self.files,
            "tables": self.tables,
            "warnings": self.warnings,
            "details": self.details,
        }
        if self.error_type:
            data["error_type"] = self.error_type
        return data
