"""Windows compatibility guards.

The owner runs Windows. These tests exist because the first version of this
codebase crashed there, on the very first command, for a reason that is
invisible on Mac and Linux.

When you call ``open()`` or ``read_text()`` without saying which text encoding
to use, Python falls back to whatever the operating system prefers. That is
UTF-8 on Mac and Linux, and something else on Windows. ``store/schema.sql`` and
``config/config.yaml`` both contain section marks and em dashes in their
comments, so on Windows those files failed to read and ``cli.py init-db`` died
with a UnicodeDecodeError that said nothing about the real cause.

The failure is reproducible on Linux by forcing an ASCII locale, which is what
these tests do. Without it, no amount of testing on this machine would ever
catch the problem — which is exactly how it got shipped in the first place.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Forces Python's default text encoding to ASCII instead of UTF-8:
#   LC_ALL=C              ask for the POSIX locale
#   PYTHONCOERCECLOCALE=0 stop Python quietly upgrading that to C.UTF-8
#   PYTHONUTF8=0          stop Python's UTF-8 mode overriding the locale
# Together these stand in for a Windows machine's default behaviour.
ASCII_LOCALE_ENV = {
    "LC_ALL": "C",
    "PYTHONCOERCECLOCALE": "0",
    "PYTHONUTF8": "0",
    "PYTHONIOENCODING": "utf-8",   # so the child can still print its own output
}


def _run_with_ascii_locale(code: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("LANG", "LC_CTYPE")}
    env.update(ASCII_LOCALE_ENV)
    env["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120,
    )


# -- proof the hazard is real ------------------------------------------------


@pytest.mark.parametrize("relative_path", ["store/schema.sql", "config/config.yaml"])
def test_runtime_files_contain_non_ascii(relative_path):
    """These two files are read on every startup and both hold non-ASCII text.

    If this ever fails it means someone stripped the characters out, and the
    tests below stop proving anything — they would pass whether or not the
    encoding was stated.
    """
    content = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
    assert any(ord(ch) > 127 for ch in content)


def test_the_ascii_locale_actually_breaks_a_bare_read():
    """Confirms the simulation works on this machine.

    Without this check, a change in how Python handles locales could silently
    turn every test below into a no-op that passes for the wrong reason.
    """
    result = _run_with_ascii_locale(
        "import pathlib, sys\n"
        "try:\n"
        "    pathlib.Path('store/schema.sql').read_text()\n"  # encoding-guard: intentional
        "    sys.exit(1)\n"
        "except UnicodeDecodeError:\n"
        "    sys.exit(0)\n"
    )
    assert result.returncode == 0, (
        "Expected a bare read_text() to fail under an ASCII locale. It did not, "
        "so these Windows guards are not testing anything.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


# -- the actual guards -------------------------------------------------------


def test_config_loads_under_an_ascii_locale():
    result = _run_with_ascii_locale(
        "from config import load_config\n"
        "cfg = load_config()\n"
        "assert cfg.universe.min_market_cap_usd == 10_000_000_000\n"
        "print('ok')\n"
    )
    assert result.returncode == 0, f"config/config.yaml failed to load:\n{result.stderr}"


def test_database_schema_applies_under_an_ascii_locale():
    """This is the one that used to kill ``cli.py init-db`` on Windows."""
    result = _run_with_ascii_locale(
        "from store import connect, table_counts\n"
        "conn = connect(':memory:')\n"
        "assert len(table_counts(conn)) == 14\n"
        "print('ok')\n"
    )
    assert result.returncode == 0, f"store/schema.sql failed to load:\n{result.stderr}"


def test_decision_log_writes_under_an_ascii_locale(tmp_path):
    """Traces must survive a non-UTF-8 machine too.

    Headlines and company names carry accents and dashes routinely, so a log
    that cannot write them would fail on exactly the days worth investigating.
    """
    result = _run_with_ascii_locale(
        "from store import connect, DecisionLog\n"
        f"conn = connect(':memory:')\n"
        f"log = DecisionLog(conn, mode='test', log_dir=r'{tmp_path}')\n"
        "log.screen('NKE', stage='shock', passed=True, note='caf\\u00e9 \\u2014 na\\u00efve')\n"
        "log.finish()\n"
        "print('ok')\n"
    )
    assert result.returncode == 0, f"DecisionLog failed to write:\n{result.stderr}"

    written = list(tmp_path.glob("*.jsonl"))
    assert len(written) == 1
    assert "run_finished" in written[0].read_text(encoding="utf-8")


def test_the_full_offline_flow_runs_under_an_ascii_locale(tmp_path):
    """End to end: create a database, seed it, and run a backtest.

    Roughly what a first-time Windows user does in their first five minutes.
    """
    db_path = tmp_path / "windows_sim.duckdb"
    result = _run_with_ascii_locale(
        "from datetime import date\n"
        "from store import connect\n"
        "from ingest.demo_data import seed_demo\n"
        "from backtest import BacktestEngine, BuyAndHold, reference_buy_and_hold\n"
        f"conn = connect(r'{db_path}')\n"
        "seed_demo(conn)\n"
        "start, end = date(2021, 1, 4), date(2023, 12, 29)\n"
        "res = BacktestEngine(conn, starting_cash=100_000.0).run(BuyAndHold('SPY'), start, end)\n"
        "ref = reference_buy_and_hold(conn, 'SPY', start, end, 100_000.0)\n"
        "gap = float((res.equity_curve['equity'] - ref['equity']).abs().max())\n"
        "assert gap == 0.0, gap\n"
        "print('ok')\n"
    )
    assert result.returncode == 0, f"Offline flow failed under ASCII locale:\n{result.stderr}"


# -- cheap static guard ------------------------------------------------------

# Matches a text-mode file read or write that does not state its encoding.
_UNSAFE_IO = re.compile(
    r"\.(?:read_text|write_text|open)\((?![^)]*encoding=)(?![^)]*[\"']rb[\"'])"
    r"(?![^)]*[\"']wb[\"'])[^)]*\)"
)

_SCANNED_DIRS = ("config", "store", "ingest", "screens", "backtest", "tests")


def test_no_file_io_omits_its_encoding():
    """Catches the mistake at the source, before it needs a subprocess to find.

    The tests above only cover the paths they happen to exercise. This one
    covers every file, which matters because the next place someone reads a
    file will be written by whoever picks this up months from now.
    """
    offenders: list[str] = []
    paths = [REPO_ROOT / "cli.py"]
    for directory in _SCANNED_DIRS:
        paths.extend((REPO_ROOT / directory).rglob("*.py"))

    for path in paths:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            # The marker is for the one place above that omits the encoding on
            # purpose, to prove the ASCII-locale simulation still bites.
            if (
                "duckdb.connect" in line
                or "encoding-guard: intentional" in line
                or line.lstrip().startswith("#")
            ):
                continue
            if _UNSAFE_IO.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}")

    assert not offenders, (
        "These read or write a file without saying which encoding to use, which "
        "works on Mac and Linux and breaks on Windows:\n  " + "\n  ".join(offenders)
    )
