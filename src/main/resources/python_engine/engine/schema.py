"""Schema metadata model and the pure reflection-dict -> TableMeta conversion.

Everything here is free of database I/O so it can be exercised with hand-written
reflection dicts. `inspector.py` supplies the real dicts from SQLAlchemy.
"""

from dataclasses import dataclass, field

from . import naming
from .type_mapper import MapperContext, map_type

# Columns carrying these server defaults are auto-populated by the database.
_AUTOINCREMENT_MARKERS = ("nextval(", "auto_increment", "autoincrement")


@dataclass
class ColumnMeta:
    name: str
    attr_name: str
    python_type: str
    sa_type_expr: str
    emit_sa_type: bool
    nullable: bool
    primary_key: bool
    autoincrement: bool
    unique: bool = False
    index: bool = False
    foreign_key: str = None
    server_default: str = None
    comment: str = None
    name_differs: bool = False
    unmapped: bool = False
    type_notes: tuple = ()
    sa_imports: tuple = ()
    py_imports: tuple = ()

    def mapped_column_args(self, emit_comments=True):
        """Render the positional/keyword arguments for `mapped_column(...)`.

        Argument order matters: `mapped_column` accepts (name, type, *SchemaItem),
        so an explicit column name must come *before* the type. A string in the
        type position is routed into `*arg`, which expects SchemaItem objects and
        raises ArgumentError.
        """
        args = []
        if self.name_differs:
            args.append(repr(self.name))
        if self.emit_sa_type and self.sa_type_expr:
            args.append(self.sa_type_expr)
        if self.foreign_key:
            args.append(f'ForeignKey("{self.foreign_key}")')
        if self.primary_key:
            args.append("primary_key=True")
            if self.autoincrement:
                args.append("autoincrement=True")
        elif not self.nullable:
            # nullable defaults to True, so only the non-null case needs stating.
            args.append("nullable=False")
        if self.unique:
            args.append("unique=True")
        if self.index and not self.unique:
            args.append("index=True")
        if self.server_default:
            args.append(f"server_default=text({self.server_default!r})")
        if emit_comments and self.comment:
            args.append(f"comment={self.comment!r}")
        return args

    @property
    def annotation(self):
        """`Mapped[str]` / `Mapped[str | None]` per the SQLAlchemy 2.0 style guide."""
        if self.primary_key or not self.nullable:
            return f"Mapped[{self.python_type}]"
        return f"Mapped[{self.python_type} | None]"

    @property
    def needs_text_import(self):
        return bool(self.server_default)


@dataclass
class ForeignKeyMeta:
    constrained_columns: list
    referred_table: str
    referred_columns: list
    referred_schema: str = None
    name: str = None

    @property
    def target(self):
        if self.referred_schema:
            return f"{self.referred_schema}.{self.referred_table}.{self.referred_columns[0]}"
        return f"{self.referred_table}.{self.referred_columns[0]}"

    @property
    def is_composite(self):
        return len(self.constrained_columns) > 1


@dataclass
class IndexMeta:
    name: str
    columns: list
    unique: bool = False


@dataclass
class RelationshipMeta:
    attr_name: str
    target_class: str
    target_table: str
    is_collection: bool
    back_populates: str = ""
    foreign_key_columns: list = field(default_factory=list)
    is_self_referential: bool = False
    # Column on the *other* table to point remote_side at (self-references only).
    remote_side_columns: list = field(default_factory=list)
    # Lazy string form, e.g. '"[Order.user_id]"', when >1 join path exists.
    foreign_keys_expr: str = ""
    # Module stem holding the target class; filled in by the renderer so
    # multi-file output can emit TYPE_CHECKING imports.
    target_module: str = ""

    def render(self):
        """Return (annotation, kwargs_source) for the relationship() call."""
        args = []
        if self.back_populates:
            args.append(f'back_populates="{self.back_populates}"')
        if self.foreign_keys_expr:
            args.append(f"foreign_keys={self.foreign_keys_expr}")
        if self.is_self_referential and self.remote_side_columns:
            cols = ", ".join(f"{self.target_class}.{c}" for c in self.remote_side_columns)
            args.append(f'remote_side="[{cols}]"')
        if self.is_collection:
            annotation = f'Mapped[list["{self.target_class}"]]'
        else:
            annotation = f'Mapped["{self.target_class}"]'
        return annotation, ", ".join(args)


@dataclass
class TableMeta:
    name: str
    class_name: str
    module_name: str
    schema: str = None
    comment: str = None
    columns: list = field(default_factory=list)
    primary_keys: list = field(default_factory=list)
    foreign_keys: list = field(default_factory=list)
    indexes: list = field(default_factory=list)
    relationships: list = field(default_factory=list)
    table_args: list = field(default_factory=list)
    # sqlalchemy names required only by __table_args__ (Index / UniqueConstraint).
    extra_sa_imports: tuple = ()

    @property
    def has_composite_pk(self):
        return len(self.primary_keys) > 1

    def column(self, name):
        for col in self.columns:
            if col.name == name:
                return col
        return None


def _is_autoincrement(col, is_pk):
    """Detect DB-generated surrogate keys across dialects."""
    if not is_pk:
        return False
    # Explicit hint from the caller (mock metadata, or a user override).
    explicit = col.get("autoincrement")
    if explicit is not None:
        return bool(explicit) and str(explicit).lower() != "false"
    extra = str(col.get("extra") or "").lower()
    if any(marker in extra for marker in _AUTOINCREMENT_MARKERS):
        return True
    default = str(col.get("default") or col.get("server_default") or "").lower()
    if any(marker in default for marker in _AUTOINCREMENT_MARKERS):
        return True
    # GENERATED { ALWAYS | BY DEFAULT } AS IDENTITY (PostgreSQL, MySQL 8)
    identity = col.get("identity")
    if isinstance(identity, dict) and identity:
        return True
    if isinstance(identity, str) and identity.upper().startswith("ALWAYS"):
        return True
    return False


def _server_default_literal(col):
    """Normalise a reflected server default into a Python source literal."""
    value = col.get("server_default", col.get("default"))
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    # Strip a trailing type cast: CURRENT_TIMESTAMP -> CURRENT_TIMESTAMP,
    # but keep `'0'::character varying` recognisable as a literal.
    if text.upper().startswith("NULL"):
        return None
    return text


def _collect_single_column_flags(unique_constraints, indexes):
    """Map column name -> (is_unique, is_indexed) for single-column constraints.

    Multi-column constraints stay in `__table_args__` instead.
    """
    unique_cols, index_cols = set(), set()
    multi_unique, multi_index = [], []

    for constraint in unique_constraints or []:
        cols = list(constraint.get("column_names") or [])
        cols = [c for c in cols if c]
        if len(cols) == 1:
            unique_cols.add(cols[0])
        elif len(cols) > 1:
            multi_unique.append((constraint.get("name"), cols))

    for index in indexes or []:
        cols = list(index.get("column_names") or [])
        cols = [c for c in cols if c]
        if index.get("unique"):
            if len(cols) == 1:
                unique_cols.add(cols[0])
                continue
        elif len(cols) == 1:
            index_cols.add(cols[0])
        if len(cols) > 1:
            multi_index.append((index.get("name"), cols, bool(index.get("unique"))))

    return unique_cols, index_cols, multi_unique, multi_index


def _render_table_args(schema, multi_unique, multi_index):
    args = []
    for name, cols in multi_unique:
        rendered = ", ".join(repr(c) for c in cols)
        args.append(f"UniqueConstraint({rendered}, name={name!r})" if name else f"UniqueConstraint({rendered})")
    for name, cols, unique in multi_index:
        rendered = ", ".join(repr(c) for c in cols)
        parts = [rendered]
        if name:
            parts.append(f"name={name!r}")
        if unique:
            parts.append("unique=True")
        args.append(f"Index({', '.join(parts)})")
    if schema:
        args.append(f"{{'schema': {schema!r}}}")
    return args


def build_table_meta(
    table_name,
    columns,
    primary_key=None,
    foreign_keys=None,
    indexes=None,
    unique_constraints=None,
    comment=None,
    schema=None,
    ctx=None,
    prefixes=(),
    escape_builtins=False,
    include_comments=True,
    class_name_override=None,
    singularize_tables=True,
):
    """Convert SQLAlchemy reflection dicts into a fully-resolved TableMeta."""
    ctx = ctx or MapperContext()
    if isinstance(primary_key, dict):
        pk_cols = list(primary_key.get("constrained_columns") or [])
    else:
        pk_cols = list(primary_key or [])
    pk_set = set(pk_cols)

    unique_cols, index_cols, multi_unique, multi_index = _collect_single_column_flags(
        unique_constraints, indexes
    )

    fk_lookup = {}
    resolved_fks = []
    for fk in foreign_keys or []:
        constrained = [c for c in (fk.get("constrained_columns") or []) if c]
        referred_table = fk.get("referred_table") or ""
        referred_columns = [c for c in (fk.get("referred_columns") or []) if c]
        if not constrained or not referred_table or not referred_columns:
            continue
        meta = ForeignKeyMeta(
            constrained_columns=constrained,
            referred_table=referred_table,
            referred_columns=referred_columns,
            referred_schema=fk.get("referred_schema") or None,
            name=fk.get("name"),
        )
        resolved_fks.append(meta)
        if not meta.is_composite:
            fk_lookup[constrained[0]] = meta

    class_name = class_name_override or naming.make_class_name(
        table_name, prefixes, escape_builtins, singularize_tables
    )
    module_name = naming.make_module_name(table_name, prefixes)

    taken = set()
    column_metas = []
    for col in columns or []:
        raw_name = col.get("name")
        if not raw_name:
            continue
        is_pk = raw_name in pk_set
        mapping = map_type(col.get("type"), ctx, table_name, raw_name)
        autoinc = _is_autoincrement(col, is_pk)
        fk = fk_lookup.get(raw_name)

        attr_name, renamed = naming.make_identifier(raw_name, escape_builtins=escape_builtins)
        # Two columns can normalise to the same attribute ("userId" and "user_id").
        attr_name = naming.unique_name(attr_name, taken)
        taken.add(attr_name)

        sa_imports = mapping.sa_imports if mapping.should_emit_sa_type else ()
        if fk is not None:
            sa_imports = tuple(sa_imports) + ("ForeignKey",)

        column_metas.append(
            ColumnMeta(
                name=raw_name,
                attr_name=attr_name,
                python_type=mapping.python_type,
                sa_type_expr=mapping.sa_type if mapping.should_emit_sa_type else "",
                emit_sa_type=mapping.should_emit_sa_type,
                nullable=bool(col.get("nullable", True)) and not is_pk,
                primary_key=is_pk,
                autoincrement=autoinc,
                unique=raw_name in unique_cols,
                index=raw_name in index_cols,
                # A DB-generated surrogate key must not also carry server_default;
                # autoincrement=True already renders it.
                foreign_key=fk.target if fk is not None else None,
                server_default=None if (is_pk and autoinc) else _server_default_literal(col),
                comment=(col.get("comment") if include_comments else None),
                name_differs=renamed or attr_name != raw_name,
                unmapped=mapping.unmapped,
                type_notes=mapping.notes,
                sa_imports=sa_imports,
                py_imports=mapping.py_imports,
            )
        )

    table_args = _render_table_args(schema, multi_unique, multi_index)
    sa_extra = set()
    if multi_unique:
        sa_extra.add("UniqueConstraint")
    if multi_index:
        sa_extra.add("Index")

    return TableMeta(
        name=table_name,
        class_name=class_name,
        module_name=module_name,
        schema=schema or None,
        comment=comment if include_comments else None,
        columns=column_metas,
        primary_keys=pk_cols,
        foreign_keys=resolved_fks,
        indexes=[
            IndexMeta(
                name=idx.get("name") or "",
                columns=[c for c in (idx.get("column_names") or []) if c],
                unique=bool(idx.get("unique")),
            )
            for idx in (indexes or [])
        ],
        table_args=table_args,
        extra_sa_imports=tuple(sorted(sa_extra)),
    )


def tables_to_summary(tables):
    """Compact table list for the `list_tables` action (no column detail)."""
    return [
        {
            "name": t.name,
            "schema": t.schema,
            "class_name": t.class_name,
            "module_name": t.module_name,
            "comment": t.comment,
            "column_count": len(t.columns),
            "primary_keys": list(t.primary_keys),
            "foreign_key_count": len(t.foreign_keys),
        }
        for t in tables
    ]
