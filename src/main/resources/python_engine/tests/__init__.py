"""Test package bootstrap: puts python_engine on sys.path and shares fixtures.

Tests are written against `unittest` so they run with the standard library
alone (`python -m unittest discover`); pytest collects them unchanged.
"""

import json
import os
import sys

ENGINE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

if ENGINE_ROOT not in sys.path:
    sys.path.insert(0, ENGINE_ROOT)

SAMPLE_METADATA_PATH = os.path.join(FIXTURE_DIR, "sample_metadata.json")
ALL_TABLES = ["users", "orders", "order_items", "employees", "shipments", "addresses"]


def load_sample_metadata():
    with open(SAMPLE_METADATA_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def make_config(mode="multiple", tables=None, **generation_overrides):
    """Build a config dict that runs fully offline against the sample fixture."""
    generation = {
        "mode": mode,
        "tables": tables if tables is not None else ALL_TABLES,
        "mock_metadata": load_sample_metadata(),
    }
    generation.update(generation_overrides)
    return {
        "action": "generate",
        "connection": {"dialect": "mysql", "host": "localhost", "database": "shop"},
        "generation": generation,
    }
