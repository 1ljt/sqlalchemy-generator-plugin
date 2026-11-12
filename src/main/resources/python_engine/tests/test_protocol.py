"""JSON wire protocol: config parsing, validation, URL building, result shape."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import protocol
from engine.protocol import (
    Config,
    ConnectionConfig,
    GenerationConfig,
    Result,
    ERR_MISSING_DEPENDENCIES,
    EXIT_MISSING_DEPS,
    EXIT_OK,
)


class TestConnectionConfig(unittest.TestCase):
    def test_default_port_per_dialect(self):
        self.assertEqual(ConnectionConfig.from_dict({"dialect": "mysql"}).port, 3306)
        self.assertEqual(ConnectionConfig.from_dict({"dialect": "postgresql"}).port, 5432)
        self.assertEqual(ConnectionConfig.from_dict({"dialect": "mariadb"}).port, 3306)

    def test_explicit_port_wins(self):
        self.assertEqual(ConnectionConfig.from_dict({"dialect": "mysql", "port": 3307}).port, 3307)

    def test_string_port_is_coerced(self):
        self.assertEqual(ConnectionConfig.from_dict({"port": "5433"}).port, 5433)

    def test_garbage_port_falls_back_to_dialect_default(self):
        self.assertEqual(ConnectionConfig.from_dict({"dialect": "mysql", "port": "abc"}).port, 3306)

    def test_dialect_is_lowercased(self):
        self.assertEqual(ConnectionConfig.from_dict({"dialect": "MySQL"}).dialect, "mysql")

    def test_none_values_use_defaults(self):
        config = ConnectionConfig.from_dict({"host": None, "user": None, "port": None})
        self.assertEqual(config.host, "localhost")
        self.assertEqual(config.user, "")

    def test_non_dict_input_is_tolerated(self):
        self.assertIsInstance(ConnectionConfig.from_dict(None), ConnectionConfig)
        self.assertIsInstance(ConnectionConfig.from_dict("nonsense"), ConnectionConfig)


class TestUrlBuilding(unittest.TestCase):
    def test_mysql_url(self):
        config = ConnectionConfig(dialect="mysql", host="db.example.com", port=3306,
                                  database="shop", user="root", password="secret")
        self.assertEqual(config.build_url(), "mysql+pymysql://root:secret@db.example.com:3306/shop")

    def test_postgres_url(self):
        config = ConnectionConfig(dialect="postgresql", host="localhost", port=5432,
                                  database="app", user="postgres", password="pw")
        self.assertEqual(config.build_url(), "postgresql+psycopg2://postgres:pw@localhost:5432/app")

    def test_password_with_special_characters_is_escaped(self):
        # A '@' or '/' in the password would otherwise break URL parsing.
        config = ConnectionConfig(dialect="mysql", host="h", database="d",
                                  user="u", password="p@ss/w:rd")
        url = config.build_url()
        self.assertIn("p%40ss%2Fw%3Ard", url)
        self.assertNotIn("p@ss/w:rd", url)

    def test_empty_password_omits_colon(self):
        config = ConnectionConfig(dialect="mysql", host="h", database="d", user="u", password="")
        self.assertIn("u@h", config.build_url())
        self.assertNotIn(":@", config.build_url())

    def test_charset_becomes_query_param(self):
        config = ConnectionConfig(dialect="mysql", host="h", database="d", charset="utf8mb4")
        self.assertTrue(config.build_url().endswith("?charset=utf8mb4"))

    def test_scalar_extra_params_become_query_params(self):
        config = ConnectionConfig(dialect="mysql", host="h", database="d",
                                  extra_params={"ssl_disabled": "true"})
        self.assertIn("ssl_disabled=true", config.build_url())

    def test_dict_extra_params_go_to_connect_args_not_url(self):
        config = ConnectionConfig(dialect="mysql", host="h", database="d",
                                  extra_params={"ssl": {"ca": "/tmp/ca.pem"}})
        self.assertNotIn("ssl", config.build_url())
        self.assertEqual(config.connect_args(), {"ssl": {"ca": "/tmp/ca.pem"}})

    def test_unsupported_dialect_raises(self):
        with self.assertRaises(ValueError):
            ConnectionConfig(dialect="oracle").build_url()


class TestPasswordRedaction(unittest.TestCase):
    def test_to_dict_redacts_password(self):
        config = ConnectionConfig(dialect="mysql", password="hunter2")
        self.assertEqual(config.to_dict()["password"], "***")
        self.assertNotIn("hunter2", str(config.to_dict()))

    def test_to_dict_can_keep_password_for_internal_use(self):
        config = ConnectionConfig(password="hunter2")
        self.assertEqual(config.to_dict(redact=False)["password"], "hunter2")

    def test_empty_password_not_replaced_with_stars(self):
        self.assertEqual(ConnectionConfig(password="").to_dict()["password"], "")


class TestGenerationConfig(unittest.TestCase):
    def test_defaults(self):
        config = GenerationConfig.from_dict({})
        self.assertEqual(config.mode, "multiple")
        self.assertEqual(config.base_class_name, "Base")
        self.assertTrue(config.generate_base_file)
        self.assertTrue(config.generate_init)
        self.assertTrue(config.use_future_annotations)
        self.assertTrue(config.generate_relationships)
        self.assertTrue(config.singularize_class_names)
        self.assertFalse(config.use_dialect_types)

    def test_invalid_mode_falls_back_to_multiple(self):
        self.assertEqual(GenerationConfig.from_dict({"mode": "sideways"}).mode, "multiple")

    def test_mode_is_case_insensitive(self):
        self.assertEqual(GenerationConfig.from_dict({"mode": "SINGLE"}).mode, "single")

    def test_string_table_list_is_wrapped(self):
        self.assertEqual(GenerationConfig.from_dict({"tables": "users"}).tables, ["users"])

    def test_prefix_list_accepts_single_string(self):
        self.assertEqual(GenerationConfig.from_dict({"table_prefix_strip": "t_"}).table_prefix_strip, ["t_"])

    def test_non_list_tables_is_empty(self):
        self.assertEqual(GenerationConfig.from_dict({"tables": 42}).tables, [])

    def test_base_import_disables_base_file_generation(self):
        config = GenerationConfig.from_dict({"base_import": "from myapp.db import Base"})
        self.assertEqual(config.base_import, "from myapp.db import Base")
        self.assertFalse(config.generate_base_file)

    def test_type_override_dicts_survive(self):
        config = GenerationConfig.from_dict({
            "type_overrides": {"user.status": {"python_type": "str"}},
            "custom_type_map": {"JSON": {"sa_type": "JSON"}},
        })
        self.assertIn("user.status", config.type_overrides)
        self.assertIn("JSON", config.custom_type_map)

    def test_malformed_dict_fields_become_empty(self):
        config = GenerationConfig.from_dict({"type_overrides": "nope", "custom_type_map": 7})
        self.assertEqual(config.type_overrides, {})
        self.assertEqual(config.custom_type_map, {})


class TestConfigValidation(unittest.TestCase):
    def test_valid_generate_config(self):
        config = Config.from_dict({
            "action": "generate",
            "connection": {"dialect": "mysql", "host": "h", "database": "d"},
            "generation": {"tables": ["users"]},
        })
        self.assertEqual(config.validate(), [])

    def test_missing_database_reported(self):
        config = Config.from_dict({"action": "generate",
                                   "connection": {"host": "h"},
                                   "generation": {"tables": ["t"]}})
        self.assertTrue(any("database" in p for p in config.validate()))

    def test_missing_tables_reported_for_generate(self):
        config = Config.from_dict({"action": "generate",
                                   "connection": {"host": "h", "database": "d"}})
        self.assertTrue(any("tables" in p for p in config.validate()))

    def test_unsupported_dialect_reported(self):
        config = Config.from_dict({"action": "test_connection",
                                   "connection": {"dialect": "oracle", "host": "h", "database": "d"}})
        self.assertTrue(any("oracle" in p for p in config.validate()))

    def test_mock_metadata_skips_connection_requirements(self):
        config = Config.from_dict({
            "action": "generate",
            "generation": {"tables": ["t"], "mock_metadata": {"t": {"columns": []}}},
        })
        self.assertEqual(config.validate(), [])

    def test_unknown_action_rejected(self):
        with self.assertRaises(ValueError):
            Config.from_dict({"action": "delete_everything"})

    def test_non_object_config_rejected(self):
        with self.assertRaises(ValueError):
            Config.from_dict([1, 2, 3])

    def test_action_defaults_to_generate(self):
        self.assertEqual(Config.from_dict({}).action, "generate")

    def test_template_dir_defaults_to_empty(self):
        self.assertEqual(Config.from_dict({}).template_dir, "")


class TestResult(unittest.TestCase):
    def test_success_shape(self):
        result = Result.success("generate", message="ok", files=[{"relative_path": "a.py"}])
        data = result.to_dict()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["action"], "generate")
        self.assertNotIn("error_type", data)
        self.assertEqual(result.exit_code, EXIT_OK)

    def test_error_shape_includes_error_type(self):
        result = Result.error("generate", ERR_MISSING_DEPENDENCIES, "missing packages",
                              details={"missing": ["sqlalchemy"]})
        data = result.to_dict()
        self.assertEqual(data["status"], "error")
        self.assertEqual(data["error_type"], ERR_MISSING_DEPENDENCIES)
        self.assertEqual(data["details"]["missing"], ["sqlalchemy"])
        self.assertEqual(result.exit_code, EXIT_MISSING_DEPS)

    def test_error_without_known_type_uses_generic_exit(self):
        result = Result.error("generate", "something_odd", "boom")
        self.assertEqual(result.exit_code, protocol.EXIT_ERROR)

    def test_files_accept_generated_file_objects(self):
        generated = protocol.GeneratedFile(relative_path="m.py", content="x = 1", kind="model",
                                           table_names=["t"])
        result = Result.success("generate", files=[generated])
        self.assertEqual(result.to_dict()["files"][0]["relative_path"], "m.py")

    def test_result_is_json_serialisable(self):
        import json
        result = Result.success("generate", files=[{"relative_path": "a.py", "content": "é中文"}])
        # ASCII-escaped by default so a charset mismatch cannot corrupt the payload.
        encoded = json.dumps(result.to_dict())
        self.assertTrue(encoded.isascii())
        self.assertEqual(json.loads(encoded)["files"][0]["content"], "é中文")


if __name__ == "__main__":
    unittest.main()
