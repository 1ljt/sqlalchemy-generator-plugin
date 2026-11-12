"""Dependency detection: the 'explicit prompt + optional one-click install' path."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import dependencies


class TestFindMissing(unittest.TestCase):
    def test_installed_packages_report_none_missing(self):
        self.assertEqual(dependencies.find_missing(["sqlalchemy", "jinja2", "json", "os"]), [])

    def test_absent_package_is_reported(self):
        self.assertEqual(dependencies.find_missing(["definitely_not_installed_xyz"]),
                         ["definitely_not_installed_xyz"])

    def test_mixed_set_returns_only_the_absent(self):
        missing = dependencies.find_missing(["sqlalchemy", "definitely_not_installed_xyz"])
        self.assertEqual(missing, ["definitely_not_installed_xyz"])

    def test_empty_input(self):
        self.assertEqual(dependencies.find_missing([]), [])


class TestCheck(unittest.TestCase):
    def test_ok_when_core_dependencies_present(self):
        # No dialect => only sqlalchemy + jinja2 are required, both are vendored
        # prerequisites of this test suite.
        result = dependencies.check(None)
        self.assertTrue(result["ok"], result)

    def test_mysql_requires_pymysql(self):
        self.assertIn("pymysql", dependencies.DIALECT_DEPENDENCIES["mysql"])
        result = dependencies.check("mysql")
        if result["ok"]:
            self.assertEqual(dependencies.find_missing(["pymysql"]), [])
        else:
            self.assertIn("pymysql", result["missing"])

    def test_mariadb_uses_the_mysql_driver(self):
        self.assertEqual(dependencies.DIALECT_DEPENDENCIES["mariadb"],
                         dependencies.DIALECT_DEPENDENCIES["mysql"])

    def test_postgresql_requires_psycopg2(self):
        self.assertIn("psycopg2", dependencies.DIALECT_DEPENDENCIES["postgresql"])

    def test_unknown_dialect_only_checks_core(self):
        result = dependencies.check("cockroachdb")
        self.assertTrue(result["ok"], result)


class TestFailurePayload(unittest.TestCase):
    """The Kotlin side renders this payload directly into a dialog."""

    def setUp(self):
        self.original = dict(dependencies.CORE_DEPENDENCIES)
        dependencies.CORE_DEPENDENCIES["absent_pkg_xyz"] = "absent-dist-xyz"

    def tearDown(self):
        dependencies.CORE_DEPENDENCIES.clear()
        dependencies.CORE_DEPENDENCIES.update(self.original)

    def test_reports_missing_import_names(self):
        result = dependencies.check(None)
        self.assertFalse(result["ok"])
        self.assertIn("absent_pkg_xyz", result["missing"])

    def test_reports_pypi_distribution_names(self):
        # Import name and distribution name differ; pip needs the latter.
        result = dependencies.check(None)
        self.assertIn("absent-dist-xyz", result["missing_distributions"])

    def test_install_command_is_copy_pasteable(self):
        result = dependencies.check(None)
        command = result["install_command"]
        self.assertIn("-m pip install", command)
        self.assertIn("absent-dist-xyz", command)
        # Quoting the interpreter path survives spaces (C:\Program Files\...).
        self.assertTrue(command.startswith('"'))
        self.assertIn(sys.executable, command)

    def test_exposes_the_interpreter_being_used(self):
        result = dependencies.check(None)
        self.assertEqual(result["interpreter"], sys.executable)
        self.assertTrue(result["python_version"])

    def test_payload_is_json_serialisable(self):
        import json
        result = dependencies.check(None)
        self.assertEqual(json.loads(json.dumps(result)), result)


class TestDistributionNamesAreInstallable(unittest.TestCase):
    def test_every_distribution_name_looks_like_a_pypi_package(self):
        names = set(dependencies.CORE_DEPENDENCIES.values())
        for table in dependencies.DIALECT_DEPENDENCIES.values():
            names.update(table.values())
        for name in names:
            with self.subTest(distribution=name):
                self.assertRegex(name, r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

    def test_psycopg2_maps_to_the_binary_wheel(self):
        # Plain `psycopg2` needs a compiler + libpq headers; the -binary wheel
        # is what a one-click install must use.
        self.assertEqual(dependencies.DIALECT_DEPENDENCIES["postgresql"]["psycopg2"],
                         "psycopg2-binary")


if __name__ == "__main__":
    unittest.main()
