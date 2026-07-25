"""Opening the DuckDB file and applying the schema.

DuckDB is a single file on disk with no server to run or configure. Deleting
``data/market.duckdb`` throws away everything and costs nothing but a re-ingest.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def resolve_db_path(db_path: str | Path) -> Path:
    """Interpret a configured path relative to the repo, not the shell's cwd.

    So ``python cli.py`` behaves the same whichever directory it is run from —
    which matters once cron is the thing running it.
    """
    path = Path(db_path)
    return path if path.is_absolute() else REPO_ROOT / path


def connect(
    db_path: str | Path = "data/market.duckdb",
    *,
    read_only: bool = False,
    apply_schema: bool = True,
) -> duckdb.DuckDBPyConnection:
    """Open the store, creating it and its schema on first use.

    Pass ``db_path=":memory:"`` for tests: same schema, nothing touches disk.
    """
    if str(db_path) == ":memory:":
        conn = duckdb.connect(":memory:")
    else:
        resolved = resolve_db_path(db_path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        conn = duckdb.connect(str(resolved), read_only=read_only)

    if apply_schema and not read_only:
        init_schema(conn)
    return conn


def init_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Apply schema.sql. Safe to run repeatedly — every statement is IF NOT EXISTS.

    The encoding is stated explicitly, and must stay that way. Python picks the
    operating system's default text encoding when you leave it out, which is
    UTF-8 on Mac and Linux but not on Windows. schema.sql contains section marks
    and em dashes in its comments, so omitting it makes this line raise
    UnicodeDecodeError on a Windows machine and nowhere else — the first command
    a new Windows user runs would fail with an error that says nothing about the
    real cause.
    """
    conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))


def table_counts(conn: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Row count per table, for the health section of the digest and the CLI."""
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' ORDER BY table_name"
        ).fetchall()
    ]
    return {
        name: conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
        for name in tables
    }
