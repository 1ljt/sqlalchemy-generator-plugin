"""End-to-end tests for the CLI contract and the generated code.

These are the tests that matter most: they run generator.py exactly as the IDE
plugin will, then prove the emitted Python is not merely syntactically valid but
is loadable SQLAlchemy 2.0 -- mappers configure, DDL compiles, and relationship
navigation works against a real (SQLite) database.
"""

import ast
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import ALL_TABLES, ENGINE_ROOT, load_sample_metadata, make_config

GENERATOR = os.path.join(ENGINE_ROOT, "generator.py")

_import_counter = 0


def run_cli(config_dict=None, extra_args=(), config_text=None):
    """Invoke generator.py in a subprocess exactly as the Kotlin side will."""
    workdir = tempfile.mkdtemp(prefix="sa_cli_")
    config_path = os.path.join(workdir, "config.json")
    if config_text is not None:
        with open(config_path, "w", encoding="utf-8") as handle:
            handle.write(config_text)
    else:
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(config_dict or {}, handle, ensure_ascii=False)

    env = dict(os.environ)
    # Without these the child falls back to the console codepage on Windows and
    # CJK text in tracebacks/comments cannot be decoded. The Kotlin side must
    # set the same variables on its GeneralCommandLine.
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    try:
        proc = subprocess.run(
            [sys.executable, GENERATOR, "--config", config_path, *extra_args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=180,
        )
        return proc.returncode, proc.stdout, proc.stderr
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def write_package(files, directory, package_name):
    """Materialise generated files as an importable package."""
    pkg_dir = os.path.join(directory, package_name)
    os.makedirs(pkg_dir, exist_ok=True)
    for entry in files:
        with open(os.path.join(pkg_dir, entry["relative_path"]), "w", encoding="utf-8") as handle:
            handle.write(entry["content"])
    return pkg_dir


def import_package(pkg_dir, parent_dir):
    """Import the package under a unique name and return the module."""
    global _import_counter
    _import_counter += 1
    name = f"gen_pkg_{_import_counter}_{uuid.uuid4().hex[:8]}"
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(pkg_dir, "__init__.py"), submodule_search_locations=[pkg_dir]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return name, module


def unload_package(name):
    for key in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
        del sys.modules[key]


class TestCLIOkPath(unittest.TestCase):
    def test_stdout_is_pure_json(self):
        """The plugin parses stdout as JSON; any stray byte breaks it."""
        code, stdout, _ = run_cli(make_config(mode="multiple"))
        self.assertEqual(code, 0, stdout)
        payload = json.loads(stdout)  # raises if stdout is not exactly one JSON doc
        self.assertEqual(payload["status"], "success")

    def test_stderr_never_contains_json_result(self):
        code, stdout, stderr = run_cli(make_config(mode="multiple"))
        self.assertEqual(code, 0)
        self.assertNotIn('"status": "success"', stderr)

    def test_result_file_mode_keeps_stdout_empty(self):
        workdir = tempfile.mkdtemp(prefix="sa_rf_")
        try:
            config_path = os.path.join(workdir, "config.json")
            result_path = os.path.join(workdir, "result.json")
            with open(config_path, "w", encoding="utf-8") as handle:
                json.dump(make_config(mode="multiple"), handle)
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
            proc = subprocess.run(
                [sys.executable, GENERATOR, "--config", config_path, "--result-file", result_path],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                env=env, timeout=180,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "")
            with open(result_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            self.assertEqual(payload["status"], "success")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def test_pretty_output_keeps_utf8_literals(self):
        config = make_config(mode="single", tables=["users"])
        _, stdout, _ = run_cli(config, extra_args=["--pretty"])
        # --pretty is for humans: CJK stays readable rather than \uXXXX escaped.
        self.assertIn("系统用户表", stdout)
        self.assertEqual(json.loads(stdout)["status"], "success")

    def test_default_output_is_ascii_safe(self):
        config = make_config(mode="single", tables=["users"])
        _, stdout, _ = run_cli(config)
        self.assertTrue(stdout.isascii(), "default IPC output must survive any reader codepage")
        self.assertIn("\\u", stdout)

    def test_version_and_describe_types(self):
        for flag in ("--version", "--describe-types"):
            code, stdout, _ = run_cli(extra_args=[flag])
            self.assertEqual(code, 0, stdout)
            self.assertEqual(json.loads(stdout)["status"], "success")

    def test_generated_files_are_syntactically_valid_python(self):
        _, stdout, _ = run_cli(make_config(mode="multiple"))
        for entry in json.loads(stdout)["files"]:
            with self.subTest(file=entry["relative_path"]):
                ast.parse(entry["content"])

    def test_warnings_are_reported_not_swallowed(self):
        metadata = load_sample_metadata()
        metadata["weird"] = {"primary_keys": ["id"],
                             "columns": [{"name": "id", "db_type": "INTEGER", "nullable": False},
                                         {"name": "blob_col", "db_type": "GEOMETRY_XYZ", "nullable": True}]}
        config = make_config(mode="multiple", tables=["weird"], mock_metadata=metadata)
        code, stdout, _ = run_cli(config)
        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertTrue(payload["warnings"])
        self.assertIn("weird.blob_col", payload["details"]["unmapped_columns"])


class TestGeneratedCodeIsUsableSQLAlchemy(unittest.TestCase):
    """Beyond syntax: does SQLAlchemy actually accept the generated models?"""

    def assertPackageLoads(self, mode):
        workdir = tempfile.mkdtemp(prefix="sa_load_")
        name = None
        try:
            _, stdout, _ = run_cli(make_config(mode=mode))
            payload = json.loads(stdout)
            self.assertEqual(payload["status"], "success", payload.get("message"))
            pkg_dir = write_package(payload["files"], workdir, "models_pkg")
            name, module = import_package(pkg_dir, workdir)

            # Configure only *this* registry. The global configure_mappers()
            # would also touch registries left over from earlier tests.
            module.Base.registry.configure()

            classes = [getattr(module, n) for n in module.__all__]
            self.assertTrue(classes)
            for cls in classes:
                if getattr(cls, "__table__", None) is None:
                    continue
                with self.subTest(cls=cls.__name__):
                    self.assertTrue(cls.__table__.columns)
            return module, workdir, name
        except Exception:
            shutil.rmtree(workdir, ignore_errors=True)
            if name:
                unload_package(name)
            raise

    def test_multiple_file_mode_configures_mappers(self):
        module, workdir, name = self.assertPackageLoads("multiple")
        try:
            self.assertIn("User", module.__all__)
            self.assertIn("Order", module.__all__)
            self.assertTrue(issubclass(module.User, module.Base))
        finally:
            unload_package(name)
            shutil.rmtree(workdir, ignore_errors=True)

    def test_single_file_mode_configures_mappers(self):
        module, workdir, name = self.assertPackageLoads("single")
        try:
            self.assertTrue(issubclass(module.User, module.Base))
            self.assertTrue(issubclass(module.Order, module.Base))
        finally:
            unload_package(name)
            shutil.rmtree(workdir, ignore_errors=True)

    def test_ddl_compiles_for_mysql(self):
        from sqlalchemy.dialects import mysql
        from sqlalchemy.schema import CreateTable

        workdir = tempfile.mkdtemp(prefix="sa_ddl_")
        name = None
        try:
            _, stdout, _ = run_cli(make_config(mode="multiple"))
            payload = json.loads(stdout)
            pkg_dir = write_package(payload["files"], workdir, "models_pkg")
            name, module = import_package(pkg_dir, workdir)
            for cls_name in ("User", "Order", "OrderItem", "Employee", "Address"):
                table = getattr(module, cls_name).__table__
                ddl = str(CreateTable(table).compile(dialect=mysql.dialect()))
                self.assertIn(f"CREATE TABLE {table.name}", ddl)
        finally:
            if name:
                unload_package(name)
            shutil.rmtree(workdir, ignore_errors=True)


class TestRelationshipRoundTrip(unittest.TestCase):
    """Insert real rows and walk the relationships both ways."""

    def test_one_to_many_and_self_reference(self):
        workdir = tempfile.mkdtemp(prefix="sa_rt_")
        name = None
        engine = None
        try:
            _, stdout, _ = run_cli(make_config(
                mode="multiple", tables=["users", "orders", "employees"],
                include_column_comments=False,
            ))
            payload = json.loads(stdout)
            self.assertEqual(payload["status"], "success", payload.get("message"))
            pkg_dir = write_package(payload["files"], workdir, "models_pkg")
            name, module = import_package(pkg_dir, workdir)

            from sqlalchemy import create_engine
            from sqlalchemy.orm import Session

            engine = create_engine("sqlite://")
            module.Base.metadata.create_all(engine)

            with Session(engine) as session:
                user = module.User(user_name="alice", is_active=True, balance=0)
                session.add(user)
                session.flush()

                # `orders.id` is BIGINT. Only INTEGER PRIMARY KEY aliases the
                # rowid in SQLite, so BIGINT keys need explicit values here;
                # MySQL and PostgreSQL auto-increment them normally.
                session.add(module.Order(id=1, user_id=user.id, status="paid", total=10))
                session.add(module.Order(id=2, user_id=user.id, status="draft", total=5))
                session.flush()

                boss = module.Employee(full_name="Boss")
                session.add(boss)
                session.flush()
                minion = module.Employee(full_name="Minion", manager_id=boss.id)
                session.add(minion)
                session.commit()

                session.expunge_all()
                fresh_user = session.query(module.User).filter_by(user_name="alice").one()
                self.assertEqual(len(fresh_user.orders), 2)
                self.assertEqual({o.status for o in fresh_user.orders}, {"paid", "draft"})
                for order in fresh_user.orders:
                    self.assertEqual(order.user.user_name, "alice")

                fresh_minion = session.query(module.Employee).filter_by(full_name="Minion").one()
                self.assertEqual(fresh_minion.manager.full_name, "Boss")
                fresh_boss = session.query(module.Employee).filter_by(full_name="Boss").one()
                self.assertEqual([e.full_name for e in fresh_boss.employees], ["Minion"])
        finally:
            if engine is not None:
                engine.dispose()
            if name:
                unload_package(name)
            shutil.rmtree(workdir, ignore_errors=True)

    def test_multiple_foreign_keys_between_one_pair(self):
        workdir = tempfile.mkdtemp(prefix="sa_multi_")
        name = None
        engine = None
        try:
            _, stdout, _ = run_cli(make_config(
                mode="multiple", tables=["shipments", "addresses"],
                include_column_comments=False,
            ))
            payload = json.loads(stdout)
            pkg_dir = write_package(payload["files"], workdir, "models_pkg")
            name, module = import_package(pkg_dir, workdir)

            from sqlalchemy import create_engine
            from sqlalchemy.orm import Session

            engine = create_engine("sqlite://")
            module.Base.metadata.create_all(engine)

            with Session(engine) as session:
                home = module.Address(line1="1 Home St")
                work = module.Address(line1="2 Work Ave")
                session.add_all([home, work])
                session.flush()
                shipment = module.Shipment(
                    shipping_address_id=home.id, billing_address_id=work.id
                )
                session.add(shipment)
                session.commit()
                session.expunge_all()

                loaded = session.query(module.Shipment).one()
                self.assertEqual(loaded.shipping_address.line1, "1 Home St")
                self.assertEqual(loaded.billing_address.line1, "2 Work Ave")
                # Re-query: expunge_all() detached the earlier instances, and a
                # detached object cannot lazy-load its collections.
                home = session.query(module.Address).filter_by(line1="1 Home St").one()
                work = session.query(module.Address).filter_by(line1="2 Work Ave").one()
                self.assertEqual(len(home.shipping_address_shipments), 1)
                self.assertEqual(len(work.billing_address_shipments), 1)
        finally:
            if engine is not None:
                engine.dispose()
            if name:
                unload_package(name)
            shutil.rmtree(workdir, ignore_errors=True)


class TestGeneratedStyle(unittest.TestCase):
    """The hard requirements from the SQLAlchemy 2.0 style spec."""

    @classmethod
    def setUpClass(cls):
        _, stdout, _ = run_cli(make_config(mode="multiple"))
        cls.files = {e["relative_path"]: e["content"] for e in json.loads(stdout)["files"]}

    def test_no_legacy_column_declarations(self):
        for path, content in self.files.items():
            if path == "__init__.py":
                continue
            with self.subTest(file=path):
                self.assertNotIn("= Column(", content)
                self.assertNotIn("declarative_base()", content)

    def test_uses_modern_orm_imports(self):
        content = self.files["users.py"]
        self.assertIn("from sqlalchemy.orm import", content)
        self.assertIn("Mapped", content)
        self.assertIn("mapped_column", content)

    def test_uses_declarative_base_class(self):
        self.assertIn("class Base(DeclarativeBase):", self.files["base.py"])
        self.assertIn("from sqlalchemy.orm import DeclarativeBase", self.files["base.py"])

    def test_mapped_column_style(self):
        content = self.files["users.py"]
        self.assertIn("id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True", content)
        self.assertIn("user_name: Mapped[str] = mapped_column('userName', String(50), nullable=False", content)
        self.assertIn("email: Mapped[str | None] = mapped_column(String(255)", content)

    def test_nullable_uses_pep604_union_syntax(self):
        self.assertIn("Mapped[str | None]", self.files["users.py"])
        self.assertNotIn("Optional[", self.files["users.py"])

    def test_from_future_annotations_present(self):
        for path, content in self.files.items():
            with self.subTest(file=path):
                self.assertTrue(content.startswith("from __future__ import annotations")
                                or content.startswith('"""'))

    def test_multi_file_mode_uses_type_checking_for_cross_module_targets(self):
        content = self.files["orders.py"]
        self.assertIn("if TYPE_CHECKING:", content)
        self.assertIn("from .users import User", content)
        self.assertIn('Mapped["User"]', content)

    def test_single_file_mode_needs_no_type_checking_block(self):
        _, stdout, _ = run_cli(make_config(mode="single"))
        files = {e["relative_path"]: e["content"] for e in json.loads(stdout)["files"]}
        self.assertNotIn("TYPE_CHECKING", files["models.py"])

    def test_multiple_mode_emits_snake_case_module_per_table(self):
        for expected in ("users.py", "orders.py", "order_items.py"):
            self.assertIn(expected, self.files)

    def test_init_exports_every_model(self):
        content = self.files["__init__.py"]
        for name in ("Base", "User", "Order", "OrderItem", "Employee", "Shipment", "Address"):
            self.assertIn(f'"{name}"', content)
        self.assertIn("from .base import Base", content)

    def test_two_blank_lines_between_classes_in_single_mode(self):
        _, stdout, _ = run_cli(make_config(mode="single"))
        files = {e["relative_path"]: e["content"] for e in json.loads(stdout)["files"]}
        self.assertIn("\n\n\nclass ", files["models.py"])

    def test_no_unused_sqlalchemy_type_imports(self):
        # `Integer` is omitted from mapped_column() for int columns, so it must
        # not be imported either.
        content = self.files["users.py"]
        import_line = [line for line in content.splitlines()
                       if line.startswith("from sqlalchemy import ")][0]
        imported = {n.strip() for n in import_line.split("import ", 1)[1].split(",")}
        for name in imported:
            if name == "text":
                continue
            with self.subTest(symbol=name):
                # Every imported symbol must actually appear in a mapped_column.
                body = content.split("class ", 1)[1]
                self.assertIn(name, body)


class TestCLIErrorPaths(unittest.TestCase):
    def assertError(self, code, stdout, expected_type, expected_exit):
        self.assertEqual(code, expected_exit, stdout)
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["error_type"], expected_type)
        self.assertTrue(payload["message"])

    def test_missing_config_flag(self):
        workdir = tempfile.mkdtemp(prefix="sa_nc_")
        try:
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
            proc = subprocess.run([sys.executable, GENERATOR], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", env=env, timeout=60)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["error_type"], "invalid_config")
            self.assertNotEqual(proc.returncode, 0)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def test_nonexistent_config_file(self):
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        proc = subprocess.run(
            [sys.executable, GENERATOR, "--config", os.path.join(tempfile.gettempdir(), "nope.json")],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=60,
        )
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["error_type"], "invalid_config")
        self.assertIn("not found", payload["message"])

    def test_malformed_json_config(self):
        code, stdout, _ = run_cli(config_text="{ not valid json ")
        self.assertError(code, stdout, "invalid_config", 4)

    def test_unknown_action(self):
        code, stdout, _ = run_cli({"action": "drop_database"})
        self.assertError(code, stdout, "invalid_config", 4)

    def test_generate_without_tables(self):
        code, stdout, _ = run_cli({"action": "generate",
                                   "generation": {"mock_metadata": {"t": {"columns": []}}}})
        self.assertError(code, stdout, "invalid_config", 4)
        self.assertIn("tables", json.loads(stdout)["message"])

    def test_unsupported_dialect(self):
        code, stdout, _ = run_cli({"action": "test_connection",
                                   "connection": {"dialect": "oracle", "host": "h", "database": "d"}})
        self.assertError(code, stdout, "invalid_config", 4)

    def test_mock_table_absent_from_metadata(self):
        config = make_config(mode="multiple", tables=["does_not_exist"])
        code, stdout, _ = run_cli(config)
        self.assertError(code, stdout, "generation_failed", 5)

    def test_connection_failure_is_classified(self):
        # Port 1 on loopback refuses immediately on every platform.
        config = {
            "action": "test_connection",
            "connection": {"dialect": "mysql", "host": "127.0.0.1", "port": 1,
                           "database": "nope", "user": "u", "password": "p"},
        }
        code, stdout, _ = run_cli(config)
        self.assertError(code, stdout, "connection_failed", 3)
        payload = json.loads(stdout)
        # The password must never be echoed back to the IDE logs.
        self.assertNotIn("\"p\"", json.dumps(payload["details"]))
        self.assertEqual(payload["details"]["connection"]["password"], "***")

    def test_password_never_appears_in_stdout_or_stderr(self):
        config = {
            "action": "list_tables",
            "connection": {"dialect": "mysql", "host": "127.0.0.1", "port": 1,
                           "database": "nope", "user": "u", "password": "Sup3rS3cret!"},
        }
        _, stdout, stderr = run_cli(config)
        self.assertNotIn("Sup3rS3cret!", stdout)
        self.assertNotIn("Sup3rS3cret!", stderr)


class TestTestConnectionAction(unittest.TestCase):
    def test_offline_list_tables_uses_mock_metadata(self):
        config = {
            "action": "list_tables",
            "generation": {"tables": ["users", "orders"], "mock_metadata": load_sample_metadata()},
        }
        code, stdout, _ = run_cli(config)
        self.assertEqual(code, 0, stdout)
        payload = json.loads(stdout)
        names = sorted(t["name"] for t in payload["tables"])
        self.assertEqual(names, ["orders", "users"])
        user = next(t for t in payload["tables"] if t["name"] == "users")
        self.assertEqual(user["class_name"], "User")
        self.assertEqual(user["column_count"], 8)


class TestGenerationOptions(unittest.TestCase):
    def render(self, **generation):
        _, stdout, _ = run_cli(make_config(mode="multiple", tables=["users"], **generation))
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "success", payload.get("message"))
        return {e["relative_path"]: e["content"] for e in payload["files"]}

    def test_custom_base_class_name(self):
        files = self.render(base_class_name="DatabaseBase", base_file_name="db_base.py")
        self.assertIn("class DatabaseBase(DeclarativeBase):", files["db_base.py"])
        self.assertIn("class User(DatabaseBase):", files["users.py"])
        self.assertIn("from .db_base import DatabaseBase", files["users.py"])

    def test_user_supplied_base_import_suppresses_base_file(self):
        files = self.render(base_import="from myapp.db import Base")
        self.assertNotIn("base.py", files)
        self.assertIn("from myapp.db import Base", files["users.py"])
        self.assertNotIn("DeclarativeBase", files["users.py"])

    def test_single_file_name_is_honoured(self):
        _, stdout, _ = run_cli(make_config(mode="single", tables=["users"],
                                           single_file_name="orm_models.py"))
        paths = [e["relative_path"] for e in json.loads(stdout)["files"]]
        self.assertIn("orm_models.py", paths)
        self.assertNotIn("users.py", paths)

    def test_table_prefix_stripping(self):
        metadata = {"t_user": load_sample_metadata()["users"]}
        _, stdout, _ = run_cli(make_config(mode="multiple", tables=["t_user"],
                                           mock_metadata=metadata, table_prefix_strip=["t_"]))
        payload = json.loads(stdout)
        paths = [e["relative_path"] for e in payload["files"]]
        self.assertIn("user.py", paths)
        content = next(e["content"] for e in payload["files"] if e["relative_path"] == "user.py")
        self.assertIn("class User(", content)
        self.assertIn('__tablename__ = "t_user"', content)

    def test_docstrings_can_be_disabled(self):
        with_docs = self.render(include_docstrings=True)["users.py"]
        without = self.render(include_docstrings=False, include_column_comments=False)["users.py"]
        self.assertIn("系统用户表", with_docs)
        self.assertNotIn("系统用户表", without)

    def test_singularisation_can_be_disabled(self):
        content = self.render(singularize_class_names=False)["users.py"]
        self.assertIn("class Users(", content)

    def test_dialect_types_can_be_enabled(self):
        metadata = load_sample_metadata()
        _, stdout, _ = run_cli(make_config(
            mode="multiple", tables=["addresses"], mock_metadata=metadata, use_dialect_types=True,
        ))
        content = next(e["content"] for e in json.loads(stdout)["files"]
                       if e["relative_path"] == "addresses.py")
        self.assertIn("JSONB", content)
        self.assertIn("from sqlalchemy.dialects.postgresql import JSONB", content)

    def test_enum_as_native(self):
        _, stdout, _ = run_cli(make_config(mode="multiple", tables=["orders"], enum_as_native=True))
        content = next(e["content"] for e in json.loads(stdout)["files"]
                       if e["relative_path"] == "orders.py")
        self.assertIn("Enum(", content)
        self.assertIn("'draft'", content)

    def test_type_override_is_applied(self):
        _, stdout, _ = run_cli(make_config(
            mode="multiple", tables=["users"],
            type_overrides={"users.email": {"python_type": "str", "sa_type": "String(320)"}},
        ))
        content = next(e["content"] for e in json.loads(stdout)["files"]
                       if e["relative_path"] == "users.py")
        self.assertIn("String(320)", content)

    def test_relationships_can_be_disabled(self):
        content = self.render(generate_relationships=False)["users.py"]
        self.assertNotIn("relationship(", content)
        self.assertNotIn("TYPE_CHECKING", content)

    def test_init_generation_can_be_disabled(self):
        files = self.render(generate_init=False)
        self.assertNotIn("__init__.py", files)

    def test_class_name_override(self):
        content = self.render(class_name_overrides={"users": "Account"})["users.py"]
        self.assertIn("class Account(", content)


class TestCustomTemplates(unittest.TestCase):
    def test_user_template_directory_is_honoured(self):
        workdir = tempfile.mkdtemp(prefix="sa_tpl_")
        try:
            template_dir = os.path.join(workdir, "templates")
            shutil.copytree(os.path.join(ENGINE_ROOT, "templates"), template_dir)
            # Stamp a recognisable marker into the model template.
            model_path = os.path.join(template_dir, "model.py.j2")
            with open(model_path, "r", encoding="utf-8") as handle:
                original = handle.read()
            with open(model_path, "w", encoding="utf-8") as handle:
                handle.write("{{ header }}\n# CUSTOM TEMPLATE MARKER\n" + original.split("{{ header }}", 1)[1])

            config = make_config(mode="multiple", tables=["users"])
            config["template_dir"] = template_dir
            code, stdout, _ = run_cli(config)
            self.assertEqual(code, 0, stdout)
            content = next(e["content"] for e in json.loads(stdout)["files"]
                           if e["relative_path"] == "users.py")
            self.assertIn("# CUSTOM TEMPLATE MARKER", content)
            ast.parse(content)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
