"""Inspector: offline metadata loading and live-connection error handling."""

import importlib.util
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import inspector
from engine.inspector import DatabaseError, create_engine_for, tables_from_mock
from engine.protocol import Config, ConnectionConfig, GenerationConfig
from engine.type_mapper import MapperContext


def make_generation(**overrides):
    return GenerationConfig.from_dict(overrides)


class TestCreateEngine(unittest.TestCase):
    def test_mysql_engine_url(self):
        engine = create_engine_for(ConnectionConfig.from_dict({
            "dialect": "mysql", "host": "h", "port": 3306,
            "database": "d", "user": "u", "password": "hunter2",
        }))
        try:
            self.assertEqual(engine.url.drivername, "mysql+pymysql")
            self.assertEqual(engine.url.database, "d")
            self.assertEqual(engine.url.port, 3306)
            # str(url) must hide the password -- it ends up in logs and reprs.
            self.assertNotIn("hunter2", str(engine.url))
            self.assertIn("hunter2", engine.url.render_as_string(hide_password=False))
        finally:
            engine.dispose()

    def test_postgres_engine(self):
        psycopg2 = importlib.util.find_spec("psycopg2")
        if psycopg2 is None:
            self.skipTest("psycopg2 is not installed in this environment")
        engine = create_engine_for(ConnectionConfig.from_dict({
            "dialect": "postgresql", "host": "h", "database": "d", "user": "u",
        }))
        try:
            self.assertEqual(engine.url.drivername, "postgresql+psycopg2")
        finally:
            engine.dispose()

    def test_postgres_url_is_built_even_without_the_driver(self):
        # URL construction must not require psycopg2; only connect() does.
        config = ConnectionConfig.from_dict({
            "dialect": "postgresql", "host": "h", "database": "d", "user": "u",
        })
        self.assertTrue(config.build_url().startswith("postgresql+psycopg2://"))

    def test_missing_driver_reports_an_actionable_database_error(self):
        """A missing/broken DBAPI driver must not escape as ModuleNotFoundError."""
        config = ConnectionConfig(dialect="postgresql", host="h", database="d", user="u")
        if importlib.util.find_spec("psycopg2") is not None:
            self.skipTest("psycopg2 is installed; cannot exercise the missing-driver path")
        with self.assertRaises(DatabaseError) as ctx:
            create_engine_for(config)
        message = str(ctx.exception)
        self.assertIn("pip install", message)
        self.assertIn("psycopg2", message)

    def test_unsupported_dialect_raises_database_error(self):
        with self.assertRaises(DatabaseError):
            create_engine_for(ConnectionConfig(dialect="oracle", host="h", database="d"))

    def test_connect_args_passed_through(self):
        engine = create_engine_for(ConnectionConfig.from_dict({
            "dialect": "mysql", "host": "h", "database": "d",
            "extra_params": {"ssl": {"ca": "/tmp/ca.pem"}},
        }))
        try:
            self.assertIsNotNone(engine)
        finally:
            engine.dispose()


class TestConnectionFailure(unittest.TestCase):
    """Port 1 on loopback refuses immediately on every platform."""

    REFUSED = ConnectionConfig(dialect="mysql", host="127.0.0.1", port=1,
                               database="nope", user="u", password="s3cret")

    def test_test_connection_raises_database_error(self):
        with self.assertRaises(DatabaseError) as ctx:
            inspector.test_connection(self.REFUSED)
        self.assertTrue(str(ctx.exception))

    def test_list_tables_raises_database_error(self):
        with self.assertRaises(DatabaseError):
            inspector.list_tables(self.REFUSED)

    def test_inspect_tables_raises_database_error(self):
        with self.assertRaises(DatabaseError):
            inspector.inspect_tables(self.REFUSED, ["t"], MapperContext(), make_generation())

    def test_error_message_does_not_leak_the_password(self):
        with self.assertRaises(DatabaseError) as ctx:
            inspector.test_connection(self.REFUSED)
        self.assertNotIn("s3cret", str(ctx.exception))

    def test_error_message_is_single_line(self):
        # The IDE shows this verbatim in a dialog; multi-line driver dumps are noise.
        with self.assertRaises(DatabaseError) as ctx:
            inspector.test_connection(self.REFUSED)
        self.assertNotIn("\n", str(ctx.exception))


class TestTablesFromMock(unittest.TestCase):
    def setUp(self):
        self.metadata = {
            "users": {
                "comment": "用户",
                "primary_keys": ["id"],
                "columns": [
                    {"name": "id", "db_type": "INTEGER", "nullable": False, "autoincrement": True},
                    {"name": "email", "db_type": "VARCHAR(255)", "nullable": True},
                ],
            },
            "orders": {
                "primary_keys": ["id"],
                "columns": [
                    {"name": "id", "db_type": "BIGINT", "nullable": False, "autoincrement": True},
                    {"name": "user_id", "db_type": "INTEGER", "nullable": False},
                ],
                "foreign_keys": [{"constrained_columns": ["user_id"], "referred_table": "users",
                                  "referred_columns": ["id"]}],
            },
        }

    def load(self, tables, **gen_kwargs):
        gen = make_generation(tables=tables, mock_metadata=self.metadata, **gen_kwargs)
        warnings = []
        result = tables_from_mock(self.metadata, gen, MapperContext(dialect="mysql"), warnings)
        return result, warnings

    def test_builds_requested_tables_in_order(self):
        tables, warnings = self.load(["users", "orders"])
        self.assertEqual([t.name for t in tables], ["users", "orders"])
        self.assertEqual(warnings, [])

    def test_columns_are_resolved(self):
        tables, _ = self.load(["users"])
        user = tables[0]
        self.assertEqual(user.class_name, "User")
        self.assertEqual(user.comment, "用户")
        self.assertEqual([c.name for c in user.columns], ["id", "email"])
        self.assertTrue(user.column("id").primary_key)
        self.assertTrue(user.column("id").autoincrement)
        self.assertEqual(user.column("email").python_type, "str")

    def test_foreign_keys_survive(self):
        tables, _ = self.load(["users", "orders"])
        orders = next(t for t in tables if t.name == "orders")
        self.assertEqual(len(orders.foreign_keys), 1)
        self.assertEqual(orders.column("user_id").foreign_key, "users.id")

    def test_missing_table_warns_and_skips(self):
        tables, warnings = self.load(["users", "ghost"])
        self.assertEqual([t.name for t in tables], ["users"])
        self.assertTrue(any("ghost" in w for w in warnings))

    def test_unknown_db_type_warns_but_continues(self):
        self.metadata["weird"] = {
            "primary_keys": ["id"],
            "columns": [{"name": "id", "db_type": "INTEGER", "nullable": False},
                        {"name": "geo", "db_type": "GEOGRAPHY(POINT)", "nullable": True}],
        }
        tables, warnings = self.load(["weird"])
        self.assertTrue(any("GEOGRAPHY" in w for w in warnings))
        self.assertTrue(tables[0].column("geo").unmapped)

    def test_primary_key_accepts_constraint_dict_shape(self):
        self.metadata["dict_pk"] = {
            "primary_key": {"constrained_columns": ["id"]},
            "columns": [{"name": "id", "db_type": "INTEGER", "nullable": False}],
        }
        tables, _ = self.load(["dict_pk"])
        self.assertTrue(tables[0].columns[0].primary_key)

    def test_schema_is_propagated(self):
        self.metadata["users"]["schema"] = "reporting"
        tables, _ = self.load(["users"])
        self.assertEqual(tables[0].schema, "reporting")
        self.assertIn("schema", tables[0].table_args[-1])

    def test_indexes_and_unique_constraints(self):
        self.metadata["users"]["indexes"] = [
            {"name": "ix_email", "column_names": ["email"], "unique": False}]
        self.metadata["users"]["unique_constraints"] = [
            {"name": "uq_pair", "column_names": ["id", "email"]}]
        tables, _ = self.load(["users"])
        self.assertTrue(tables[0].column("email").index)
        self.assertTrue(any("UniqueConstraint" in a for a in tables[0].table_args))

    def test_prefix_stripping_and_class_override(self):
        self.metadata["t_users"] = dict(self.metadata["users"])
        tables, _ = self.load(["t_users"], table_prefix_strip=["t_"])
        self.assertEqual(tables[0].class_name, "User")
        self.assertEqual(tables[0].module_name, "users")

        tables, _ = self.load(["t_users"], class_name_overrides={"t_users": "Account"})
        self.assertEqual(tables[0].class_name, "Account")


class TestLoadTablesDispatch(unittest.TestCase):
    def test_mock_metadata_short_circuits_the_database(self):
        # A config with unreachable connection details still succeeds offline.
        config = Config.from_dict({
            "action": "generate",
            "connection": {"dialect": "mysql", "host": "127.0.0.1", "port": 1, "database": "x"},
            "generation": {
                "tables": ["users"],
                "mock_metadata": {"users": {
                    "primary_keys": ["id"],
                    "columns": [{"name": "id", "db_type": "INTEGER", "nullable": False}],
                }},
            },
        })
        warnings = []
        tables = inspector.load_tables(config, MapperContext(dialect="mysql"), warnings)
        self.assertEqual([t.name for t in tables], ["users"])

    def test_without_mock_metadata_it_attempts_a_connection(self):
        config = Config.from_dict({
            "action": "generate",
            "connection": {"dialect": "mysql", "host": "127.0.0.1", "port": 1,
                           "database": "x", "user": "u", "password": "p"},
            "generation": {"tables": ["users"]},
        })
        with self.assertRaises(DatabaseError):
            inspector.load_tables(config, MapperContext(dialect="mysql"), [])


class TestLiveReflectionAgainstSQLite(unittest.TestCase):
    """Drive the reflection path against a real database.

    SQLite stands in for MySQL/PostgreSQL so the reflection logic is exercised
    with genuine reflected type objects rather than hand-written dicts.
    """

    @classmethod
    def setUpClass(cls):
        import sqlalchemy as sa

        cls.sa = sa
        cls.engine = sa.create_engine("sqlite://")
        metadata = sa.MetaData()

        sa.Table(
            "authors", metadata,
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("name", sa.String(100), nullable=False),
            sa.Column("email", sa.String(255), nullable=True),
            sa.Column("bio", sa.Text, nullable=True),
            sa.Column("balance", sa.Numeric(10, 2), nullable=False),
            sa.Column("is_active", sa.Boolean, nullable=False),
            sa.Column("born_on", sa.Date, nullable=True),
            sa.Column("created_at", sa.DateTime, nullable=False,
                      server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.UniqueConstraint("name", "email", name="uq_author_name_email"),
            comment="写作者",
        )
        sa.Table(
            "books", metadata,
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("author_id", sa.Integer, sa.ForeignKey("authors.id"), nullable=False),
            sa.Column("title", sa.String(200), nullable=False),
            sa.Column("pages", sa.Integer, nullable=True),
        )
        metadata.create_all(cls.engine)
        with cls.engine.begin() as conn:
            conn.execute(sa.text("CREATE INDEX ix_books_title ON books (title)"))
            conn.execute(sa.text("CREATE UNIQUE INDEX ix_books_pages ON books (pages)"))
        cls.inspector = sa.inspect(cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def generation(self, **overrides):
        return make_generation(tables=["authors", "books"], **overrides)

    def test_list_tables_finds_real_tables(self):
        entries = inspector.list_tables_from_inspector(self.inspector)
        names = [e["name"] for e in entries]
        self.assertIn("authors", names)
        self.assertIn("books", names)
        self.assertEqual(names, sorted(names))
        for entry in entries:
            self.assertEqual(entry["kind"], "table")

    def test_list_tables_excludes_sqlite_internals(self):
        names = [e["name"] for e in inspector.list_tables_from_inspector(self.inspector)]
        self.assertNotIn("sqlite_sequence", names)

    def test_list_tables_include_views_flag_is_safe_without_views(self):
        entries = inspector.list_tables_from_inspector(self.inspector, include_views=True)
        self.assertTrue(entries)

    def test_reflect_authors_columns(self):
        tables = inspector.reflect_tables_from_inspector(
            self.inspector, ["authors"], None, MapperContext(dialect="mysql"), self.generation()
        )
        authors = tables[0]
        self.assertEqual(authors.class_name, "Author")
        self.assertEqual(authors.module_name, "authors")

        by_name = {c.name: c for c in authors.columns}
        self.assertEqual(by_name["id"].python_type, "int")
        self.assertTrue(by_name["id"].primary_key)
        # SQLite reflection reports no autoincrement metadata at all (no `extra`,
        # no `identity`, no sequence default), so we must NOT detect it here.
        # Guessing would wrongly stamp autoincrement=True on application-assigned
        # integer PKs. MySQL reports extra='auto_increment' and PostgreSQL reports
        # identity/nextval -- both covered in test_schema.TestAutoincrementDetection.
        # Omitting it is still correct: SQLAlchemy defaults integer single-column
        # PKs to autoincrement="auto".
        self.assertFalse(by_name["id"].autoincrement)
        self.assertNotIn("autoincrement", " ".join(by_name["id"].mapped_column_args()))
        self.assertEqual(by_name["name"].python_type, "str")
        self.assertFalse(by_name["name"].nullable)
        self.assertEqual(by_name["email"].python_type, "str")
        self.assertTrue(by_name["email"].nullable)
        self.assertEqual(by_name["bio"].python_type, "str")
        self.assertIn("Text", by_name["bio"].sa_type_expr)
        self.assertEqual(by_name["balance"].python_type, "Decimal")
        self.assertEqual(by_name["is_active"].python_type, "bool")
        self.assertEqual(by_name["born_on"].python_type, "date")
        self.assertEqual(by_name["created_at"].python_type, "datetime")

    def test_reflect_keeps_varchar_length(self):
        tables = inspector.reflect_tables_from_inspector(
            self.inspector, ["authors"], None, MapperContext(), self.generation()
        )
        name_col = next(c for c in tables[0].columns if c.name == "name")
        self.assertEqual(name_col.sa_type_expr, "String(100)")

    def test_reflect_multi_column_unique_becomes_table_args(self):
        tables = inspector.reflect_tables_from_inspector(
            self.inspector, ["authors"], None, MapperContext(), self.generation()
        )
        joined = " ".join(tables[0].table_args)
        self.assertIn("UniqueConstraint", joined)
        self.assertIn("uq_author_name_email", joined)

    def test_reflect_foreign_key(self):
        tables = inspector.reflect_tables_from_inspector(
            self.inspector, ["books"], None, MapperContext(), self.generation()
        )
        books = tables[0]
        self.assertEqual(len(books.foreign_keys), 1)
        self.assertEqual(books.column("author_id").foreign_key, "authors.id")
        self.assertIn("ForeignKey", books.column("author_id").sa_imports)

    def test_reflect_indexes(self):
        tables = inspector.reflect_tables_from_inspector(
            self.inspector, ["books"], None, MapperContext(), self.generation()
        )
        books = tables[0]
        self.assertTrue(books.column("title").index)
        # A unique index sets `unique`, not `index`.
        self.assertTrue(books.column("pages").unique)
        self.assertFalse(books.column("pages").index)

    def test_reflect_server_default(self):
        tables = inspector.reflect_tables_from_inspector(
            self.inspector, ["authors"], None, MapperContext(), self.generation()
        )
        created = next(c for c in tables[0].columns if c.name == "created_at")
        self.assertTrue(created.server_default)
        self.assertIn("CURRENT_TIMESTAMP", created.server_default.upper())

    def test_reflected_models_are_loadable_sqlalchemy(self):
        """The full chain: reflect a real DB -> render -> import -> configure."""
        from engine.protocol import GenerationConfig
        from engine.relationships import infer_relationships
        from engine.renderer import Renderer

        gen = GenerationConfig.from_dict({"mode": "multiple", "tables": ["authors", "books"]})
        tables = inspector.reflect_tables_from_inspector(
            self.inspector, ["authors", "books"], None, MapperContext(), gen
        )
        infer_relationships(tables)
        files = Renderer(gen).render(tables)

        workdir = tempfile.mkdtemp(prefix="sa_reflect_")
        pkg_name = None
        try:
            pkg_dir = os.path.join(workdir, "reflected_pkg")
            os.makedirs(pkg_dir)
            for entry in files:
                with open(os.path.join(pkg_dir, entry["relative_path"]), "w", encoding="utf-8") as fh:
                    fh.write(entry["content"])

            import importlib.util
            import uuid as _uuid
            pkg_name = f"reflected_pkg_{_uuid.uuid4().hex[:8]}"
            if workdir not in sys.path:
                sys.path.insert(0, workdir)
            spec = importlib.util.spec_from_file_location(
                pkg_name, os.path.join(pkg_dir, "__init__.py"),
                submodule_search_locations=[pkg_dir],
            )
            module = importlib.util.module_from_spec(spec)
            sys.modules[pkg_name] = module
            spec.loader.exec_module(module)

            module.Base.registry.configure()
            self.assertIn("Author", module.__all__)
            self.assertIn("Book", module.__all__)
            self.assertEqual(module.Book.__table__.name, "books")
            # The reflected FK became a navigable relationship.
            self.assertTrue(hasattr(module.Author, "books"))
            self.assertTrue(hasattr(module.Book, "author"))
        finally:
            if pkg_name:
                for key in [k for k in sys.modules
                            if k == pkg_name or k.startswith(pkg_name + ".")]:
                    del sys.modules[key]
            shutil.rmtree(workdir, ignore_errors=True)


class TestReadableErrors(unittest.TestCase):
    def test_strips_to_the_driver_message(self):
        class FakeOrig(Exception):
            def __str__(self):
                return "(pymysql.err.OperationalError) (2003, \"Can't connect\")\nmore noise"

        class FakeExc(Exception):
            orig = FakeOrig()

        message = inspector._readable(FakeExc())
        self.assertEqual(message, "(pymysql.err.OperationalError) (2003, \"Can't connect\")")

    def test_falls_back_to_class_name(self):
        class Boom(Exception):
            def __str__(self):
                return ""

        self.assertEqual(inspector._readable(Boom()), "Boom")


if __name__ == "__main__":
    unittest.main()
