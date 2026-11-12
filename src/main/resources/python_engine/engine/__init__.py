"""SQLAlchemy 2.0 model generation engine.

Stateless, CLI-driven. Communicates with the IDE plugin over JSON:
  - config  -> read from a temp JSON file (--config)
  - result  -> written to stdout (or --result-file)
  - logging -> stderr only, never stdout
"""

__version__ = "0.1.0"
