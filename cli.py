#!/usr/bin/env python3
"""Command line entry point.

One command to remember: ``python cli.py --help``. Everything else is listed
there. RUNBOOK.md explains each one in plain language.

Phase 0 commands:

    python cli.py init-db          create an empty local database
    python cli.py seed-demo        fill it with synthetic data (no API keys needed)
    python cli.py check-sources    Phase 0 viability check on the real sources
    python cli.py verify           prove the backtester reproduces buy-and-hold
    python cli.py backtest         run a backtest
    python cli.py screen           run the §4 screens on one ticker, one date
    python cli.py logs             pull the decision trace for a ticker and date
    python cli.py status           what is in the database and how fresh it is
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime

from config import load_config
from store import connect, table_counts

EXIT_OK, EXIT_FAIL = 0, 1


def _as_date(text: str) -> date:
    return datetime.strptime(text, "%Y-%m-%d").date()


def _db(args) -> str:
    return args.db or load_config().data.db_path


# -- commands ----------------------------------------------------------------


def cmd_init_db(args) -> int:
    conn = connect(_db(args))
    print(f"Database ready at {_db(args)} with {len(table_counts(conn))} tables.")
    print("Next: 'python cli.py seed-demo' for synthetic data, or "
          "'python cli.py check-sources' to test the real ones.")
    return EXIT_OK


def cmd_seed_demo(args) -> int:
    from ingest.demo_data import seed_demo

    conn = connect(_db(args))
    info = seed_demo(conn)
    print(f"Seeded {info['tickers']} synthetic tickers over {info['sessions']} "
          f"trading sessions ({info['start']} to {info['end']}).")
    print()
    print("This is invented data for exercising the plumbing. It is not market")
    print("data, and no result computed from it says anything about any strategy.")
    return EXIT_OK


def cmd_check_sources(args) -> int:
    from ingest.check_sources import FAIL, SKIP, format_report, run_all

    checks = run_all()
    print(format_report(checks))
    blocking = [c for c in checks if c.blocking and c.status in (FAIL, SKIP)]
    return EXIT_FAIL if blocking else EXIT_OK


def cmd_verify(args) -> int:
    """The Phase 0 exit criterion: the engine must reproduce buy-and-hold exactly.

    Three numbers are compared — the engine's, a plain reference loop's, and a
    closed-form calculation. All three must agree to floating-point exactness.
    """
    from backtest import (
        BacktestEngine, BuyAndHold, closed_form_buy_and_hold, reference_buy_and_hold,
    )

    cfg = load_config()
    conn = connect(_db(args))
    ticker = args.ticker or cfg.backtest.benchmark_ticker
    cash = float(cfg.backtest.starting_cash_usd)

    bounds = conn.execute(
        "SELECT min(session_date), max(session_date) FROM bars WHERE ticker = ?", [ticker]
    ).fetchone()
    if not bounds or bounds[0] is None:
        print(f"No price history for {ticker}. Run 'python cli.py seed-demo' first, "
              "or ingest real data.")
        return EXIT_FAIL

    start = args.start or bounds[0]
    end = args.end or bounds[1]

    result = BacktestEngine(conn, starting_cash=cash).run(BuyAndHold(ticker), start, end)
    engine_final = float(result.equity_curve["equity"].iloc[-1])
    reference = reference_buy_and_hold(conn, ticker, start, end, cash)
    reference_final = float(reference["equity"].iloc[-1])
    max_gap = float((result.equity_curve["equity"] - reference["equity"]).abs().max())

    print(f"Buy-and-hold reproduction check for {ticker}, {start} to {end}")
    print("-" * 60)
    print(f"  engine              {engine_final:,.6f}")
    print(f"  reference loop      {reference_final:,.6f}")

    try:
        closed_form = closed_form_buy_and_hold(conn, ticker, start, end, cash)
        print(f"  closed form         {closed_form:,.6f}")
        closed_form_gap = abs(closed_form - engine_final)
    except Exception as exc:                      # noqa: BLE001
        # Only valid for a name with no splits or dividends; say so rather than
        # failing the whole check.
        print(f"  closed form         not applicable ({exc})")
        closed_form_gap = 0.0

    print(f"  largest daily gap   {max_gap:.2e}")
    print()

    if max_gap == 0.0 and closed_form_gap == 0.0:
        print("PASS — the engine reproduces buy-and-hold exactly.")
        return EXIT_OK
    print("FAIL — the engine does not reproduce buy-and-hold exactly.")
    print("Do not trust any backtest result until this passes (PRD §8.5, §11).")
    return EXIT_FAIL


def cmd_backtest(args) -> int:
    from backtest import BacktestEngine, BuyAndHold, ExecutionSettings

    cfg = load_config()
    conn = connect(_db(args))

    if args.strategy != "buy_and_hold":
        print(f"Strategy '{args.strategy}' does not exist yet.")
        print("Phase 0 ships the engine and the benchmark only. Strategy A rules "
              "arrive in Phase 1, the LLM layer in Phase 2, Strategy B in Phase 3 "
              "(PRD §11).")
        return EXIT_FAIL

    bounds = conn.execute(
        "SELECT min(session_date), max(session_date) FROM bars WHERE ticker = ?",
        [args.ticker],
    ).fetchone()
    if not bounds or bounds[0] is None:
        print(f"No price history for {args.ticker}.")
        return EXIT_FAIL

    result = BacktestEngine(
        conn,
        starting_cash=float(cfg.backtest.starting_cash_usd),
        execution=ExecutionSettings(
            commission_per_share=float(cfg.backtest.commission_per_share_usd),
            slippage_bps=float(cfg.backtest.slippage_bps),
        ),
    ).run(BuyAndHold(args.ticker), args.start or bounds[0], args.end or bounds[1])

    print(result.summary())
    if result.unfilled:
        print(f"\n{len(result.unfilled)} order(s) never filled:")
        for item in result.unfilled[:10]:
            print(f"  {item['ticker']} {item['side']} — {item['reason']}")
    return EXIT_OK


def cmd_screen(args) -> int:
    """Run the §4 screens on one ticker as of one date, and show the workings."""
    from screens import survivability_gate, universe_screen
    from store import AsOf

    cfg = load_config()
    conn = connect(_db(args))
    view = AsOf(conn, args.date)

    universe = universe_screen(
        view, args.ticker,
        min_market_cap=float(cfg.universe.min_market_cap_usd),
        min_avg_dollar_volume=float(cfg.universe.min_avg_dollar_volume_usd),
        adv_window_days=int(cfg.universe.avg_dollar_volume_window_days),
        min_years_since_ipo=int(cfg.universe.min_years_since_ipo),
    )
    gate = survivability_gate(
        view, args.ticker,
        min_runway_months=int(cfg.survivability.min_runway_months),
    )

    print(f"{args.ticker} as of {args.date}")
    print("=" * 60)
    print("\nUniverse (§4.1)")
    for check in universe.checks:
        print(f"  [{'PASS' if check.passed else 'FAIL'}] {check.name}: {check.reason}")
    print("\nSurvivability (§4.2)")
    for check in gate.checks:
        print(f"  [{'PASS' if check.passed else 'FAIL'}] {check.name}: {check.reason}")

    both_passed = universe.passed and gate.passed
    print()
    print(f"Verdict: {'eligible for further analysis' if both_passed else 'REJECTED'}")
    return EXIT_OK


def cmd_logs(args) -> int:
    from store.decision_log import decisions_for

    conn = connect(_db(args))
    df = decisions_for(conn, ticker=args.ticker, on=args.date, run_id=args.run_id)
    if df.empty:
        print("No decisions recorded for that ticker/date.")
        return EXIT_OK
    with_pandas_width(df)
    return EXIT_OK


def with_pandas_width(df) -> None:
    import pandas as pd

    with pd.option_context("display.max_columns", None, "display.width", 200,
                           "display.max_colwidth", 60):
        print(df.to_string(index=False))


def cmd_status(args) -> int:
    conn = connect(_db(args))
    print(f"Database: {_db(args)}\n")
    print("Rows per table")
    print("-" * 40)
    for name, count in table_counts(conn).items():
        print(f"  {name:24s} {count:>10,}")

    watermarks = conn.execute(
        "SELECT source, entity, last_success_at, covered_through, last_error "
        "FROM ingest_watermarks ORDER BY source, entity"
    ).df()
    print("\nData freshness")
    print("-" * 40)
    if watermarks.empty:
        print("  nothing ingested yet")
    else:
        with_pandas_width(watermarks)

    runs = conn.execute(
        "SELECT run_id, mode, started_at, status, error FROM runs "
        "ORDER BY started_at DESC LIMIT 5"
    ).df()
    print("\nRecent runs")
    print("-" * 40)
    if runs.empty:
        print("  no runs recorded yet")
    else:
        with_pandas_width(runs)
    return EXIT_OK


# -- wiring ------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="Multi-strategy signal research platform — Phase 0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", help="path to the DuckDB file (defaults to config.yaml)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db", help="create an empty database").set_defaults(func=cmd_init_db)
    subparsers.add_parser("seed-demo", help="fill the database with synthetic data").set_defaults(func=cmd_seed_demo)
    subparsers.add_parser("check-sources", help="Phase 0 source viability check").set_defaults(func=cmd_check_sources)

    verify = subparsers.add_parser("verify", help="prove the engine reproduces buy-and-hold")
    verify.add_argument("--ticker")
    verify.add_argument("--start", type=_as_date)
    verify.add_argument("--end", type=_as_date)
    verify.set_defaults(func=cmd_verify)

    backtest = subparsers.add_parser("backtest", help="run a backtest")
    backtest.add_argument("--strategy", default="buy_and_hold")
    backtest.add_argument("--ticker", default="SPY")
    backtest.add_argument("--start", type=_as_date)
    backtest.add_argument("--end", type=_as_date)
    backtest.set_defaults(func=cmd_backtest)

    screen = subparsers.add_parser("screen", help="run the §4 screens on one ticker")
    screen.add_argument("--ticker", required=True)
    screen.add_argument("--date", required=True, type=_as_date)
    screen.set_defaults(func=cmd_screen)

    logs = subparsers.add_parser("logs", help="show the decision trace")
    logs.add_argument("--ticker")
    logs.add_argument("--date", type=_as_date)
    logs.add_argument("--run-id")
    logs.set_defaults(func=cmd_logs)

    subparsers.add_parser("status", help="database contents and data freshness").set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:                      # noqa: BLE001
        # PRD §9: fail closed and say something useful. A stack trace is still
        # printed for anything unexpected, because a silent exit is worse.
        print(f"\nERROR: {exc}\n", file=sys.stderr)
        if not isinstance(exc, (RuntimeError, ValueError, FileNotFoundError, LookupError)):
            raise
        return EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
