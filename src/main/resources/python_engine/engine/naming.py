"""Identifier conversion: database names -> valid, idiomatic Python names."""

import keyword
import re

_CAMEL_TO_SNAKE_1 = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_TO_SNAKE_2 = re.compile(r"([a-z\d])([A-Z])")
_SEPARATOR_SPLIT = re.compile(r"[_\-\s]+")
_NON_IDENTIFIER = re.compile(r"[^0-9a-zA-Z_]")

# Irregular plurals worth handling; relationship collection names read better
# with these than with a naive "+s".
_IRREGULAR_PLURALS = {
    "person": "people",
    "man": "men",
    "woman": "women",
    "child": "children",
    "mouse": "mice",
    "goose": "geese",
    "ox": "oxen",
    "foot": "feet",
    "tooth": "teeth",
    "datum": "data",
    "criterion": "criteria",
    "analysis": "analyses",
    "index": "indices",
    "matrix": "matrices",
    "vertex": "vertices",
    "appendix": "appendices",
    "status": "statuses",
    "address": "addresses",
}

# Python builtins that are legal as attribute names but shadow the builtin when
# used bare. Escaping them is opt-in -- `id` and `type` are extremely common
# column names and renaming them surprises users more than it helps.
SHADOWED_BUILTINS = frozenset({
    "id", "type", "list", "dict", "set", "str", "int", "float", "bool", "object",
    "filter", "map", "input", "format", "next", "open", "print", "sum", "min",
    "max", "abs", "all", "any", "bin", "hex", "oct", "pow", "range", "round",
    "sorted", "hash", "iter", "len", "vars", "zip", "super", "property",
    "classmethod", "staticmethod", "memoryview", "bytes", "bytearray",
    "complex", "tuple", "frozenset", "slice", "dir", "eval", "exec", "repr",
})


def to_snake_case(name):
    """`UserProfile` / `userID` / `HTTPResponse` -> `user_profile` / `user_id` / `http_response`."""
    if not name:
        return name
    text = _NON_IDENTIFIER.sub("_", name.strip())
    text = _CAMEL_TO_SNAKE_1.sub(r"\1_\2", text)
    text = _CAMEL_TO_SNAKE_2.sub(r"\1_\2", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text.lower()


def to_pascal_case(name):
    """`user_profile` -> `UserProfile`. Existing capitalisation is preserved per part."""
    if not name:
        return name
    parts = [p for p in _SEPARATOR_SPLIT.split(_NON_IDENTIFIER.sub("_", name.strip())) if p]
    if not parts:
        return ""
    return "".join(part[:1].upper() + part[1:] for part in parts)


def pluralize(name):
    """Conservative English pluralisation, sufficient for relationship collection names."""
    if not name:
        return name
    lowered = name.lower()
    if lowered in _IRREGULAR_PLURALS:
        plural = _IRREGULAR_PLURALS[lowered]
        # Preserve the caller's capitalisation style (Order -> Orders, order -> orders)
        return plural if name.islower() else plural[:1].upper() + plural[1:]
    if lowered.endswith(("s", "x", "z")) or lowered.endswith(("ch", "sh")):
        return name + "es"
    if lowered.endswith("y") and len(name) > 1 and name[-2].lower() not in "aeiou":
        return name[:-1] + "ies"
    return name + "s"


# Singularisation is deliberately narrower than pluralisation: only unambiguous
# suffixes are stripped, so words like "status", "news", "series" and "address"
# survive untouched rather than being mangled into "statu" / "new" / "serie".
#
# Note the suffixes below all drop exactly two characters ("es"). Plain "ses" is
# intentionally absent: "houses" -> "house" needs a single "s" dropped, while
# "addresses" -> "address" needs two, and the two cannot be told apart by suffix
# alone. "sses" is the subset that is unambiguous, so only that is special-cased.
_SINGULAR_ES_SUFFIXES = ("sses", "xes", "zes", "ches", "shes")

# Words whose singular and plural forms are identical, or that must not be
# touched at all. Checked before any suffix rule.
_INVARIANT = frozenset({
    "news", "series", "species", "media", "data", "information", "equipment",
    "money", "staff", "police", "fish", "sheep", "deer", "audio", "video",
    "analytics", "logistics", "electronics", "mathematics", "physics",
})


def singularize(name):
    """`users` -> `user`, `addresses` -> `address`, `categories` -> `category`.

    Returns the input unchanged when no safe rule applies.
    """
    if not name or len(name) < 3:
        return name
    lowered = name.lower()
    if lowered in _INVARIANT:
        return name
    for singular, plural in _IRREGULAR_PLURALS.items():
        if lowered == plural:
            return singular if name.islower() else singular[:1].upper() + singular[1:]
    if lowered.endswith(_SINGULAR_ES_SUFFIXES):
        return name[:-2]
    if lowered.endswith("ies") and len(name) > 3 and name[-4].lower() not in "aeiou":
        return name[:-3] + "y"
    if lowered.endswith("s") and not lowered.endswith(("ss", "us", "is")):
        return name[:-1]
    return name


def strip_prefixes(name, prefixes):
    """Remove the first matching table-name prefix (`t_user` -> `user`)."""
    if not prefixes:
        return name
    lowered = name.lower()
    for prefix in prefixes:
        if not prefix:
            continue
        if lowered.startswith(prefix.lower()) and len(name) > len(prefix):
            return name[len(prefix):]
    return name


def make_identifier(name, fallback="field", escape_builtins=False):
    """Coerce an arbitrary DB identifier into a valid Python attribute name.

    Returns `(identifier, renamed)` where `renamed` is True if the result differs
    from the original, signalling that the column name must be passed explicitly
    to `mapped_column()`.
    """
    original = name
    candidate = to_snake_case(name) if name else ""
    if not candidate:
        return fallback, True
    if candidate[0].isdigit():
        candidate = "_" + candidate
    if keyword.iskeyword(candidate) or candidate in ("True", "False", "None"):
        candidate = candidate + "_"
    elif escape_builtins and candidate in SHADOWED_BUILTINS:
        candidate = candidate + "_"
    return candidate, candidate != original


def make_class_name(table_name, prefixes=(), escape_builtins=False, singularize_tables=True):
    """`t_user_profile` -> `UserProfile`; `users` -> `User` when singularising."""
    base = strip_prefixes(table_name, prefixes)
    if singularize_tables:
        base = singularize(base)
    name = to_pascal_case(base)
    if not name:
        name = "Table"
    if name[0].isdigit():
        name = "_" + name
    if keyword.iskeyword(name) or (escape_builtins and name in SHADOWED_BUILTINS):
        name = name + "_"
    return name


def make_module_name(table_name, prefixes=()):
    """`UserProfile` -> `user_profile.py` stem."""
    base = strip_prefixes(table_name, prefixes)
    name = to_snake_case(base)
    if not name:
        name = "table"
    if name[0].isdigit():
        name = "_" + name
    if keyword.iskeyword(name):
        name = name + "_"
    return name


def unique_name(candidate, taken):
    """Disambiguate `candidate` against the `taken` set, appending a numeric suffix."""
    if candidate not in taken:
        return candidate
    counter = 2
    while f"{candidate}_{counter}" in taken:
        counter += 1
    return f"{candidate}_{counter}"
