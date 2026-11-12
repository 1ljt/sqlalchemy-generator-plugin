"""Foreign key -> relationship() inference, including self-references and the
multiple-join-path case that requires an explicit `foreign_keys=`.

`foreign_keys` and `remote_side` are emitted in SQLAlchemy's lazy string form
(`"[Order.user_id]"`) rather than as direct attribute references: the string is
resolved against the declarative registry at mapper-configuration time, so it
survives multi-file output where the referenced class lives in another module.
"""

from . import naming
from .schema import RelationshipMeta


def _fk_attr_name(fk, table):
    """`user_id` -> `user`. Falls back to the referenced table's snake name."""
    column = table.column(fk.constrained_columns[0])
    base = column.attr_name if column else fk.constrained_columns[0]
    if base.endswith("_id") and len(base) > 3:
        return base[:-3]
    if base.endswith("Id") and len(base) > 2:
        return base[:-2]
    return naming.to_snake_case(fk.referred_table)


def _pair_key(a, b):
    return tuple(sorted((a, b)))


def infer_relationships(tables, prefixes=(), escape_builtins=False):
    """Populate `.relationships` on every table in place.

    Relationships are only created when *both* ends are in `tables`; a FK to a
    table the user did not select stays a plain `ForeignKey(...)` column.
    """
    by_name = {t.name: t for t in tables}

    # Count join paths between each unordered table pair. More than one means
    # SQLAlchemy cannot infer which FK a relationship should use.
    pair_counts = {}
    for table in tables:
        for fk in table.foreign_keys:
            if fk.referred_table in by_name:
                key = _pair_key(table.name, fk.referred_table)
                pair_counts[key] = pair_counts.get(key, 0) + 1

    for table in tables:
        table.relationships = []

    for table in tables:
        target_classes = {}
        for other in tables:
            target_classes[other.name] = other.class_name

        for fk in table.foreign_keys:
            referred_name = fk.referred_table
            referred = by_name.get(referred_name)
            if referred is None or fk.is_composite:
                # Composite FKs need a custom primaryjoin; leave that to the user
                # rather than emitting something subtly wrong.
                continue

            self_ref = referred is table
            target_class = referred.class_name
            ambiguous = pair_counts.get(_pair_key(table.name, referred_name), 0) > 1

            fk_attr = None
            if fk.constrained_columns:
                col = table.column(fk.constrained_columns[0])
                fk_attr = col.attr_name if col else fk.constrained_columns[0]

            # --- many-to-one side (on `table`) ---
            many_attr = _fk_attr_name(fk, table)
            taken = _taken_names(table)
            many_attr = naming.unique_name(many_attr, taken)
            taken.add(many_attr)

            # --- one-to-many side (on `referred`) ---
            # Derive from the table stem, not the class name: plural table names
            # (`users`) are already plural, so pluralising the class would give
            # "userses". Only singular stems get pluralised.
            collection_base = table.module_name
            if not collection_base.endswith("s"):
                collection_base = naming.pluralize(collection_base)
            collection_attr = naming.unique_name(collection_base, _taken_names(referred))
            if collection_attr == collection_base and ambiguous and fk_attr:
                # Disambiguate by the FK that produced it: shipping_address_orders
                candidate = f"{many_attr}_{collection_base}"
                collection_attr = naming.unique_name(candidate, _taken_names(referred))

            foreign_keys_expr = (
                f'"[{table.class_name}.{fk_attr}]"' if (ambiguous and fk_attr) else ""
            )

            remote_side = []
            if self_ref and referred.primary_keys:
                remote_side = [
                    (referred.column(pk).attr_name if referred.column(pk) else pk)
                    for pk in referred.primary_keys
                ]

            table.relationships.append(
                RelationshipMeta(
                    attr_name=many_attr,
                    target_class=target_class,
                    target_table=referred_name,
                    is_collection=False,
                    back_populates=collection_attr,
                    foreign_key_columns=fk.constrained_columns,
                    is_self_referential=self_ref,
                    remote_side_columns=remote_side,
                    foreign_keys_expr=foreign_keys_expr,
                )
            )

            referred.relationships.append(
                RelationshipMeta(
                    attr_name=collection_attr,
                    target_class=table.class_name,
                    target_table=table.name,
                    is_collection=True,
                    back_populates=many_attr,
                    foreign_key_columns=fk.constrained_columns,
                    is_self_referential=self_ref,
                    foreign_keys_expr=foreign_keys_expr,
                )
            )

            taken.add(collection_attr)


def _taken_names(table):
    """Attribute names already in use on `table` (columns + relationships)."""
    names = {c.attr_name for c in table.columns}
    names.update(r.attr_name for r in table.relationships)
    return names
