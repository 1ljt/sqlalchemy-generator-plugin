#!/usr/bin/env python3
"""Measure engine line coverage using only the standard library.

Usage:
    python tools/measure_coverage.py

Why not `coverage.py`: it is not a runtime dependency of the plugin, and the
stdlib `trace` module cannot be used either -- its `_Ignore.names()` caches
decisions by bare module name, which silently drops whole files (engine/schema.py
measured 0% despite 34 passing tests). This tracer has no ignore logic.

Note: the in-process test modules are measured here. `tests/test_generator_e2e.py`
spawns generator.py as a subprocess, which no in-process tracer can follow; that
suite is what proves the CLI contract end to end.
"""

import ast
import os
import sys
import unittest

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
ENGINE_ROOT = os.path.dirname(TOOLS_DIR)
ENGINE_PKG = os.path.normcase(os.path.join(ENGINE_ROOT, "engine"))
sys.path.insert(0, ENGINE_ROOT)

ALL_TESTS = [
    "tests.test_naming", "tests.test_type_mapper", "tests.test_sql_types",
    "tests.test_protocol", "tests.test_schema", "tests.test_relationships",
    "tests.test_renderer", "tests.test_inspector", "tests.test_dependencies",
]

THRESHOLD = 80.0

executed = {}


def tracer(frame, event, arg):
    if event == "call":
        if os.path.normcase(frame.f_code.co_filename).startswith(ENGINE_PKG):
            return line_tracer
        return None
    return None


def line_tracer(frame, event, arg):
    if event == "line":
        executed.setdefault(os.path.normcase(frame.f_code.co_filename), set()).add(frame.f_lineno)
    return line_tracer


def executable_lines(path):
    with open(path, "r", encoding="utf-8") as handle:
        source = handle.read()
    lines = {n.lineno for n in ast.walk(ast.parse(source)) if isinstance(n, ast.stmt)}
    source_lines = source.splitlines()
    return {n for n in lines if n <= len(source_lines) and source_lines[n - 1].strip()}


def run_tests():
    loader = unittest.TestLoader()
    suite = unittest.TestSuite([loader.loadTestsFromName(n) for n in ALL_TESTS])
    return unittest.TextTestRunner(verbosity=0, stream=open(os.devnull, "w")).run(suite)


def main():
    # Run once untraced so a genuine failure is not mistaken for a coverage gap.
    precheck = run_tests()
    if not precheck.wasSuccessful():
        print(f"TESTS FAILED: {len(precheck.failures)} failures, {len(precheck.errors)} errors")
        return 1
    print(f"tests: {precheck.testsRun} run, 0 failures, {len(precheck.skipped)} skipped")

    # Drop the engine + tests modules so the traced pass re-executes their
    # module-level (import / class-definition) lines.
    for prefix in ("engine", "tests"):
        for name in [k for k in list(sys.modules) if k == prefix or k.startswith(prefix + ".")]:
            del sys.modules[name]

    sys.settrace(tracer)
    try:
        run_tests()
    finally:
        sys.settrace(None)

    rows, total_exec, total_cov = [], 0, 0
    for entry in sorted(os.listdir(os.path.join(ENGINE_ROOT, "engine"))):
        if not entry.endswith(".py"):
            continue
        path = os.path.join(ENGINE_ROOT, "engine", entry)
        norm = os.path.normcase(os.path.abspath(path))
        executable = executable_lines(path)
        covered = executable & executed.get(norm, set())
        missed = sorted(executable - covered)
        total_exec += len(executable)
        total_cov += len(covered)
        pct = 100.0 * len(covered) / len(executable) if executable else 100.0
        rows.append((entry, len(covered), len(executable), pct, missed))

    print()
    print(f"{'module':20s} {'covered':>8s} {'total':>7s} {'pct':>7s}")
    print("-" * 52)
    for name, cov, tot, pct, _ in rows:
        flag = "" if pct >= THRESHOLD else f"  <-- below {THRESHOLD:.0f}%"
        print(f"{name:20s} {cov:8d} {tot:7d} {pct:6.1f}%{flag}")
    print("-" * 52)
    total_pct = 100.0 * total_cov / total_exec if total_exec else 100.0
    print(f"{'TOTAL':20s} {total_cov:8d} {total_exec:7d} {total_pct:6.1f}%")

    print("\nUncovered lines:")
    for name, _, _, _, missed in rows:
        if missed:
            shown = ", ".join(str(m) for m in missed[:16])
            more = f" (+{len(missed) - 16})" if len(missed) > 16 else ""
            print(f"  {name}: {shown}{more}")

    below = [name for name, _, _, pct, _ in rows if pct < THRESHOLD]
    if below:
        print(f"\nFAIL: below {THRESHOLD:.0f}%: {', '.join(below)}")
        return 1
    print(f"\nOK: every engine module is at or above {THRESHOLD:.0f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
