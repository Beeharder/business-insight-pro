"""The data store, and the only sanctioned way to read from it.

Import ``AsOf`` and bind it to a date; do not query the tables directly. The
reason is in the module docstring of ``asof.py`` — direct queries are how
lookahead bias gets back in after you have removed it.
"""

from .asof import AsOf, FutureDataError, DEFAULT_DECISION_TIME
from .db import connect, init_schema, resolve_db_path, table_counts
from .decision_log import DecisionLog

__all__ = [
    "AsOf",
    "FutureDataError",
    "DEFAULT_DECISION_TIME",
    "connect",
    "init_schema",
    "resolve_db_path",
    "table_counts",
    "DecisionLog",
]
