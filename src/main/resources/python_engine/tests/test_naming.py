"""Identifier conversion rules."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import naming


class TestSnakeCase(unittest.TestCase):
    def test_camel_and_pascal(self):
        self.assertEqual(naming.to_snake_case("UserProfile"), "user_profile")
        self.assertEqual(naming.to_snake_case("userName"), "user_name")

    def test_acronyms(self):
        self.assertEqual(naming.to_snake_case("HTTPResponse"), "http_response")
        self.assertEqual(naming.to_snake_case("userID"), "user_id")
        self.assertEqual(naming.to_snake_case("XMLParser"), "xml_parser")

    def test_adjacent_acronyms_are_a_documented_limitation(self):
        # "HTTPURL" carries no case transition, so no algorithm can tell
        # HTTP+URL from HTTPU+RL. We accept the lossy result rather than guess.
        self.assertEqual(naming.to_snake_case("parseHTTPURL"), "parse_httpurl")

    def test_already_snake(self):
        self.assertEqual(naming.to_snake_case("user_name"), "user_name")
        self.assertEqual(naming.to_snake_case("id"), "id")

    def test_separators_and_collapsing(self):
        self.assertEqual(naming.to_snake_case("user-name"), "user_name")
        self.assertEqual(naming.to_snake_case("user  name"), "user_name")
        self.assertEqual(naming.to_snake_case("__weird__name__"), "weird_name")

    def test_digits_preserved(self):
        self.assertEqual(naming.to_snake_case("line1"), "line1")
        self.assertEqual(naming.to_snake_case("address2Street"), "address2_street")

    def test_empty(self):
        self.assertEqual(naming.to_snake_case(""), "")


class TestPascalCase(unittest.TestCase):
    def test_from_snake(self):
        self.assertEqual(naming.to_pascal_case("user_profile"), "UserProfile")
        self.assertEqual(naming.to_pascal_case("order_items"), "OrderItems")

    def test_preserves_existing_capitals(self):
        self.assertEqual(naming.to_pascal_case("UserProfile"), "UserProfile")
        self.assertEqual(naming.to_pascal_case("HTTPServer"), "HTTPServer")

    def test_single_segments(self):
        self.assertEqual(naming.to_pascal_case("t"), "T")
        self.assertEqual(naming.to_pascal_case("users"), "Users")


class TestPluralize(unittest.TestCase):
    def test_regular(self):
        self.assertEqual(naming.pluralize("user"), "users")
        self.assertEqual(naming.pluralize("order"), "orders")

    def test_sibilants(self):
        self.assertEqual(naming.pluralize("address"), "addresses")
        self.assertEqual(naming.pluralize("box"), "boxes")
        self.assertEqual(naming.pluralize("branch"), "branches")
        self.assertEqual(naming.pluralize("dish"), "dishes")

    def test_consonant_y(self):
        self.assertEqual(naming.pluralize("category"), "categories")
        self.assertEqual(naming.pluralize("company"), "companies")

    def test_vowel_y_stays_regular(self):
        self.assertEqual(naming.pluralize("key"), "keys")
        self.assertEqual(naming.pluralize("day"), "days")

    def test_irregular(self):
        self.assertEqual(naming.pluralize("person"), "people")
        self.assertEqual(naming.pluralize("child"), "children")
        self.assertEqual(naming.pluralize("index"), "indices")

    def test_preserves_capitalisation(self):
        self.assertEqual(naming.pluralize("User"), "Users")
        self.assertEqual(naming.pluralize("Person"), "People")


class TestSingularize(unittest.TestCase):
    def test_regular_s(self):
        self.assertEqual(naming.singularize("users"), "user")
        self.assertEqual(naming.singularize("orders"), "order")
        self.assertEqual(naming.singularize("order_items"), "order_item")

    def test_es_suffixes(self):
        self.assertEqual(naming.singularize("addresses"), "address")
        self.assertEqual(naming.singularize("boxes"), "box")
        self.assertEqual(naming.singularize("branches"), "branch")
        self.assertEqual(naming.singularize("classes"), "class")
        self.assertEqual(naming.singularize("processes"), "process")
        self.assertEqual(naming.singularize("dishes"), "dish")

    def test_plain_se_words_drop_only_the_s(self):
        # "houses" ends in "ses" like "addresses" does, but its singular is
        # "house" -- only "sses" is safe to strip "es" from.
        self.assertEqual(naming.singularize("houses"), "house")
        self.assertEqual(naming.singularize("invoices"), "invoice")
        self.assertEqual(naming.singularize("purchases"), "purchase")
        self.assertEqual(naming.singularize("licenses"), "license")
        self.assertEqual(naming.singularize("releases"), "release")

    def test_ies(self):
        self.assertEqual(naming.singularize("categories"), "category")
        self.assertEqual(naming.singularize("companies"), "company")

    def test_irregular(self):
        self.assertEqual(naming.singularize("people"), "person")
        self.assertEqual(naming.singularize("children"), "child")

    def test_unsafe_words_left_alone(self):
        # These must NOT be mangled into "statu", "new", "serie", "bus".
        self.assertEqual(naming.singularize("status"), "status")
        self.assertEqual(naming.singularize("news"), "news")
        self.assertEqual(naming.singularize("series"), "series")
        self.assertEqual(naming.singularize("species"), "species")
        self.assertEqual(naming.singularize("media"), "media")
        self.assertEqual(naming.singularize("bus"), "bus")
        self.assertEqual(naming.singularize("address"), "address")
        self.assertEqual(naming.singularize("analysis"), "analysis")

    def test_short_and_empty(self):
        self.assertEqual(naming.singularize(""), "")
        self.assertEqual(naming.singularize("id"), "id")

    def test_round_trip_is_stable(self):
        for word in ("user", "order", "category", "address", "box"):
            self.assertEqual(naming.singularize(naming.pluralize(word)), word)


class TestStripPrefixes(unittest.TestCase):
    def test_strips_matching_prefix(self):
        self.assertEqual(naming.strip_prefixes("t_user", ["t_"]), "user")
        self.assertEqual(naming.strip_prefixes("tbl_order", ["t_", "tbl_"]), "order")

    def test_case_insensitive(self):
        self.assertEqual(naming.strip_prefixes("T_User", ["t_"]), "User")

    def test_no_match_returns_original(self):
        self.assertEqual(naming.strip_prefixes("user", ["t_"]), "user")

    def test_never_strips_whole_name(self):
        # A table literally named "t_" must not become an empty string.
        self.assertEqual(naming.strip_prefixes("t_", ["t_"]), "t_")

    def test_empty_prefix_list(self):
        self.assertEqual(naming.strip_prefixes("t_user", []), "t_user")


class TestMakeIdentifier(unittest.TestCase):
    def test_keywords_are_escaped(self):
        for word in ("class", "def", "from", "import", "lambda", "global", "return"):
            identifier, renamed = naming.make_identifier(word)
            self.assertEqual(identifier, word + "_")
            self.assertTrue(renamed)

    def test_leading_digit(self):
        identifier, renamed = naming.make_identifier("1st_column")
        self.assertEqual(identifier, "_1st_column")
        self.assertTrue(renamed)

    def test_camel_converted_and_flagged(self):
        identifier, renamed = naming.make_identifier("userName")
        self.assertEqual(identifier, "user_name")
        self.assertTrue(renamed)

    def test_clean_name_not_flagged(self):
        identifier, renamed = naming.make_identifier("user_name")
        self.assertEqual(identifier, "user_name")
        self.assertFalse(renamed)

    def test_builtins_not_escaped_by_default(self):
        # `id` and `type` are ubiquitous column names; escaping them by default
        # would surprise users.
        self.assertEqual(naming.make_identifier("id")[0], "id")
        self.assertEqual(naming.make_identifier("type")[0], "type")

    def test_builtins_escaped_when_requested(self):
        self.assertEqual(naming.make_identifier("id", escape_builtins=True)[0], "id_")
        self.assertEqual(naming.make_identifier("type", escape_builtins=True)[0], "type_")

    def test_empty_falls_back(self):
        identifier, renamed = naming.make_identifier("", fallback="field")
        self.assertEqual(identifier, "field")
        self.assertTrue(renamed)


class TestMakeClassAndModuleNames(unittest.TestCase):
    def test_class_name_with_prefix_and_singularisation(self):
        self.assertEqual(naming.make_class_name("t_user_profile", ["t_"]), "UserProfile")
        self.assertEqual(naming.make_class_name("users", singularize_tables=True), "User")

    def test_class_name_without_singularisation(self):
        self.assertEqual(naming.make_class_name("users", singularize_tables=False), "Users")

    def test_class_name_leading_digit(self):
        self.assertEqual(naming.make_class_name("2024_report"), "_2024Report")

    def test_module_name(self):
        self.assertEqual(naming.make_module_name("UserProfile"), "user_profile")
        self.assertEqual(naming.make_module_name("t_order_items", ["t_"]), "order_items")

    def test_module_name_leading_digit(self):
        self.assertEqual(naming.make_module_name("2024_report"), "_2024_report")


class TestUniqueName(unittest.TestCase):
    def test_no_collision(self):
        self.assertEqual(naming.unique_name("user", {"order"}), "user")

    def test_collision_gets_suffix(self):
        self.assertEqual(naming.unique_name("user", {"user"}), "user_2")

    def test_increments_until_free(self):
        self.assertEqual(naming.unique_name("user", {"user", "user_2", "user_3"}), "user_4")


if __name__ == "__main__":
    unittest.main()
