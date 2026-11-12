"""Runtime dependency detection.

Imported *before* sqlalchemy/jinja2 so a missing package produces a structured
error instead of a raw ImportError traceback on stdout.
"""

import importlib.util
import sys

# package import-name -> PyPI distribution name
CORE_DEPENDENCIES = {
    "sqlalchemy": "sqlalchemy",
    "jinja2": "jinja2",
}

DIALECT_DEPENDENCIES = {
    "mysql": {"pymysql": "pymysql"},
    "mariadb": {"pymysql": "pymysql"},
    "postgresql": {"psycopg2": "psycopg2-binary"},
}


def find_missing(packages):
    """Return the subset of `packages` (import names) that cannot be imported."""
    missing = []
    for name in packages:
        try:
            if importlib.util.find_spec(name) is None:
                missing.append(name)
        except (ImportError, ValueError):
            # find_spec raises ImportError when a parent package is broken,
            # and ValueError for malformed __spec__; both mean "unusable".
            missing.append(name)
    return missing


def check(dialect=None):
    """Check core deps plus the driver needed for `dialect`.

    Returns a dict describing the outcome:
      {"ok": True}  or
      {"ok": False, "missing": [...], "install_command": "...", "interpreter": "..."}
    """
    required = dict(CORE_DEPENDENCIES)
    if dialect:
        required.update(DIALECT_DEPENDENCIES.get(dialect, {}))

    missing_imports = find_missing(required)
    if not missing_imports:
        return {"ok": True}

    distributions = [required[name] for name in missing_imports]
    return {
        "ok": False,
        "missing": missing_imports,
        "missing_distributions": distributions,
        "install_command": f'"{sys.executable}" -m pip install {" ".join(distributions)}',
        "interpreter": sys.executable,
        "python_version": sys.version.split()[0],
    }
