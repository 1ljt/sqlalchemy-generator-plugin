"""Foreign key -> relationship() inference."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlalchemy as sa

from engine.relationships import infer_relationships
from engine.schema import build_table_meta
from engine.type_mapper import MapperContext


def make_table(name, columns, pk=("id",), fks=(), **kwargs):
    cols = [{"name": "id", "type": sa.Integer(), "nullable": False, "autoincrement": True}]
    cols += columns
    return build_table_meta(
        name,
        cols,
        primary_key={"constrained_columns": list(pk)},
        foreign_keys=[dict(f) for f in fks],
        ctx=MapperContext(dialect="mysql"),
        **kwargs,
    )


def relationship_by_attr(table, attr):
    for rel in table.relationships:
        if rel.attr_name == attr:
            return rel
    return None


class TestSimpleForeignKey(unittest.TestCase):
    def setUp(self):
        self.users = make_table("users", [{"name": "email", "type": sa.String(100), "nullable": True}])
        self.orders = make_table(
            "orders",
            [{"name": "user_id", "type": sa.Integer(), "nullable": False}],
            fks=[{"constrained_columns": ["user_id"], "referred_table": "users",
                  "referred_columns": ["id"]}],
        )
        infer_relationships([self.users, self.orders])

    def test_many_to_one_side(self):
        rel = relationship_by_attr(self.orders, "user")
        self.assertIsNotNone(rel)
        self.assertFalse(rel.is_collection)
        self.assertEqual(rel.target_class, "User")

    def test_one_to_many_side(self):
        rel = relationship_by_attr(self.users, "orders")
        self.assertIsNotNone(rel)
        self.assertTrue(rel.is_collection)
        self.assertEqual(rel.target_class, "Order")

    def test_back_populates_is_symmetric(self):
        many = relationship_by_attr(self.orders, "user")
        one = relationship_by_attr(self.users, "orders")
        self.assertEqual(many.back_populates, one.attr_name)
        self.assertEqual(one.back_populates, many.attr_name)

    def test_annotations(self):
        many_annotation, _ = relationship_by_attr(self.orders, "user").render()
        one_annotation, _ = relationship_by_attr(self.users, "orders").render()
        self.assertEqual(many_annotation, 'Mapped["User"]')
        self.assertEqual(one_annotation, 'Mapped[list["Order"]]')

    def test_no_foreign_keys_kwarg_for_single_join_path(self):
        self.assertEqual(relationship_by_attr(self.orders, "user").foreign_keys_expr, "")

    def test_render_produces_relationship_kwargs(self):
        _, kwargs = relationship_by_attr(self.orders, "user").render()
        self.assertEqual(kwargs, 'back_populates="orders"')


class TestCollectionNaming(unittest.TestCase):
    def test_plural_table_does_not_become_orderses(self):
        users = make_table("users", [])
        orders = make_table(
            "orders", [{"name": "user_id", "type": sa.Integer(), "nullable": False}],
            fks=[{"constrained_columns": ["user_id"], "referred_table": "users",
                  "referred_columns": ["id"]}],
        )
        infer_relationships([users, orders])
        self.assertIsNotNone(relationship_by_attr(users, "orders"))
        self.assertIsNone(relationship_by_attr(users, "orderses"))

    def test_singular_table_gets_pluralised(self):
        user = make_table("user", [])
        order = make_table(
            "order", [{"name": "user_id", "type": sa.Integer(), "nullable": False}],
            fks=[{"constrained_columns": ["user_id"], "referred_table": "user",
                  "referred_columns": ["id"]}],
        )
        infer_relationships([user, order])
        self.assertIsNotNone(relationship_by_attr(user, "orders"))

    def test_snake_case_table_name(self):
        users = make_table("users", [])
        items = make_table(
            "order_items", [{"name": "user_id", "type": sa.Integer(), "nullable": False}],
            fks=[{"constrained_columns": ["user_id"], "referred_table": "users",
                  "referred_columns": ["id"]}],
        )
        infer_relationships([users, items])
        self.assertIsNotNone(relationship_by_attr(users, "order_items"))

    def test_collection_name_colliding_with_a_column_is_disambiguated(self):
        users = make_table("users", [{"name": "orders", "type": sa.String(10), "nullable": True}])
        orders = make_table(
            "orders", [{"name": "user_id", "type": sa.Integer(), "nullable": False}],
            fks=[{"constrained_columns": ["user_id"], "referred_table": "users",
                  "referred_columns": ["id"]}],
        )
        infer_relationships([users, orders])
        attrs = [r.attr_name for r in users.relationships]
        self.assertNotIn("orders", attrs)
        self.assertTrue(attrs[0].startswith("orders"))


class TestSelfReference(unittest.TestCase):
    def setUp(self):
        self.employees = make_table(
            "employees",
            [{"name": "manager_id", "type": sa.Integer(), "nullable": True}],
            fks=[{"constrained_columns": ["manager_id"], "referred_table": "employees",
                  "referred_columns": ["id"]}],
        )
        infer_relationships([self.employees])

    def test_both_sides_created(self):
        self.assertEqual(len(self.employees.relationships), 2)

    def test_many_side_is_marked_self_referential(self):
        rel = relationship_by_attr(self.employees, "manager")
        self.assertIsNotNone(rel)
        self.assertTrue(rel.is_self_referential)
        self.assertFalse(rel.is_collection)

    def test_collection_side_exists(self):
        rel = relationship_by_attr(self.employees, "employees")
        self.assertIsNotNone(rel)
        self.assertTrue(rel.is_collection)

    def test_remote_side_points_at_the_primary_key(self):
        rel = relationship_by_attr(self.employees, "manager")
        self.assertEqual(rel.remote_side_columns, ["id"])

    def test_remote_side_rendered_in_lazy_string_form(self):
        _, kwargs = relationship_by_attr(self.employees, "manager").render()
        self.assertIn('remote_side="[Employee.id]"', kwargs)

    def test_collection_side_has_no_remote_side(self):
        _, kwargs = relationship_by_attr(self.employees, "employees").render()
        self.assertNotIn("remote_side", kwargs)


class TestMultipleJoinPaths(unittest.TestCase):
    def setUp(self):
        self.addresses = make_table("addresses", [])
        self.shipments = make_table(
            "shipments",
            [{"name": "shipping_address_id", "type": sa.Integer(), "nullable": False},
             {"name": "billing_address_id", "type": sa.Integer(), "nullable": False}],
            fks=[
                {"constrained_columns": ["shipping_address_id"], "referred_table": "addresses",
                 "referred_columns": ["id"]},
                {"constrained_columns": ["billing_address_id"], "referred_table": "addresses",
                 "referred_columns": ["id"]},
            ],
        )
        infer_relationships([self.addresses, self.shipments])

    def test_distinct_many_side_names(self):
        self.assertIsNotNone(relationship_by_attr(self.shipments, "shipping_address"))
        self.assertIsNotNone(relationship_by_attr(self.shipments, "billing_address"))

    def test_distinct_collection_names(self):
        attrs = [r.attr_name for r in self.addresses.relationships]
        self.assertEqual(len(attrs), 2)
        self.assertEqual(len(set(attrs)), 2)

    def test_foreign_keys_disambiguation_emitted(self):
        rel = relationship_by_attr(self.shipments, "shipping_address")
        self.assertIn('foreign_keys="[Shipment.shipping_address_id]"', rel.render()[1])

    def test_both_sides_carry_foreign_keys(self):
        many = relationship_by_attr(self.shipments, "shipping_address")
        one = relationship_by_attr(self.addresses, many.back_populates)
        self.assertTrue(many.foreign_keys_expr)
        self.assertTrue(one.foreign_keys_expr)


class TestRelationshipsAreSkipped(unittest.TestCase):
    def test_fk_to_unselected_table_produces_no_relationship(self):
        # Only `orders` is being generated; `users` is not in the selection.
        orders = make_table(
            "orders", [{"name": "user_id", "type": sa.Integer(), "nullable": False}],
            fks=[{"constrained_columns": ["user_id"], "referred_table": "users",
                  "referred_columns": ["id"]}],
        )
        infer_relationships([orders])
        self.assertEqual(orders.relationships, [])
        # The FK column itself must survive -- only the relationship is dropped.
        self.assertEqual(orders.column("user_id").foreign_key, "users.id")

    def test_composite_fk_produces_no_relationship(self):
        # Composite FKs need a hand-written primaryjoin; emitting a guess would
        # be silently wrong.
        parent = make_table("parent", [])
        child = make_table(
            "child",
            [{"name": "a", "type": sa.Integer(), "nullable": False},
             {"name": "b", "type": sa.Integer(), "nullable": False}],
            fks=[{"constrained_columns": ["a", "b"], "referred_table": "parent",
                  "referred_columns": ["x", "y"]}],
        )
        infer_relationships([parent, child])
        self.assertEqual(child.relationships, [])
        self.assertEqual(parent.relationships, [])

    def test_malformed_fk_is_ignored(self):
        parent = make_table("parent", [])
        child = make_table(
            "child",
            [{"name": "parent_id", "type": sa.Integer(), "nullable": False}],
            fks=[{"constrained_columns": [], "referred_table": "parent", "referred_columns": ["id"]}],
        )
        infer_relationships([parent, child])
        self.assertEqual(child.relationships, [])


class TestRelationshipNaming(unittest.TestCase):
    def test_fk_column_without_id_suffix_uses_table_name(self):
        users = make_table("users", [])
        prefs = make_table(
            "prefs", [{"name": "owner", "type": sa.Integer(), "nullable": False}],
            fks=[{"constrained_columns": ["owner"], "referred_table": "users",
                  "referred_columns": ["id"]}],
        )
        infer_relationships([users, prefs])
        self.assertIsNotNone(relationship_by_attr(prefs, "users"))

    def test_relationship_name_colliding_with_a_column_is_disambiguated(self):
        users = make_table("users", [])
        orders = make_table(
            "orders",
            [{"name": "user_id", "type": sa.Integer(), "nullable": False},
             {"name": "user", "type": sa.String(20), "nullable": True}],
            fks=[{"constrained_columns": ["user_id"], "referred_table": "users",
                  "referred_columns": ["id"]}],
        )
        infer_relationships([users, orders])
        attrs = [r.attr_name for r in orders.relationships]
        self.assertNotIn("user", attrs)
        self.assertTrue(attrs[0].startswith("user"))

    def test_idempotent_when_run_twice(self):
        users = make_table("users", [])
        orders = make_table(
            "orders", [{"name": "user_id", "type": sa.Integer(), "nullable": False}],
            fks=[{"constrained_columns": ["user_id"], "referred_table": "users",
                  "referred_columns": ["id"]}],
        )
        infer_relationships([users, orders])
        first = [(r.attr_name, r.back_populates) for r in orders.relationships]
        infer_relationships([users, orders])
        second = [(r.attr_name, r.back_populates) for r in orders.relationships]
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
