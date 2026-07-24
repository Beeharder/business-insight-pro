"""Buy-and-hold, computed two independent ways.

PRD §11 makes this the Phase 0 exit criterion: *"reproduces SPY buy-and-hold
exactly"*. It sounds like a formality and is not. Running the simplest possible
strategy through the full engine and checking the answer against arithmetic
done outside the engine is the only cheap way to catch an off-by-one in the
fill timing, a split applied twice, or a dividend credited to the wrong day.
If the engine cannot get buy-and-hold right, nothing it says about a real
strategy means anything.
"""

from __future__ import annotations

from datetime import date
from typing import Sequence

import duckdb
import pandas as pd

from store.asof import AsOf

from .engine import OrderIntent
from .portfolio import Portfolio
from .trading_calendar import sessions as calendar_sessions


class BuyAndHold:
    """Buy once with everything, then never trade again."""

    def __init__(self, ticker: str = "SPY") -> None:
        self.ticker = ticker
        self.name = f"buy_and_hold_{ticker}"
        self._ordered = False

    def on_session(self, view: AsOf, portfolio: Portfolio) -> Sequence[OrderIntent]:
        if self._ordered:
            return []
        self._ordered = True
        return [OrderIntent(
            ticker=self.ticker,
            side="buy",
            notional=portfolio.cash,
            strategy=self.name,
            reason="benchmark: initial purchase",
        )]


def reference_buy_and_hold(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    start: date,
    end: date,
    starting_cash: float,
    *,
    calendar: str = "XNYS",
) -> pd.DataFrame:
    """The same thing, worked out by hand.

    Written deliberately as a flat loop that shares no code with the engine.
    It reproduces the engine's contract rather than its implementation:

    * The strategy is asked on the first session, so the fill is at the open of
      the **second** session.
    * Corporate actions apply at the start of a session, before any fill — so a
      dividend going ex on the purchase day is not received.
    * A holding that delists is closed out at its last close.
    * Positions are marked at the session close, on unadjusted prices.
    """
    session_dates = calendar_sessions(start, end, calendar)
    rows: list[dict] = []
    cash = starting_cash
    qty = 0.0
    cache: dict = {}

    for index, session in enumerate(session_dates):
        view = AsOf(conn, session, cache=cache)

        if qty > 0:
            for action in view.actions_on(ticker, session):
                if action["action_type"] == "split" and action["ratio"]:
                    qty *= float(action["ratio"])
                elif action["action_type"] == "cash_dividend" and action["cash_amount"]:
                    cash += qty * float(action["cash_amount"])

        if qty > 0 and view.is_delisted(ticker):
            last = view.bar(ticker, adjusted=False)
            if last is not None:
                cash += qty * float(last["close"])
                qty = 0.0

        if index == 1:                       # fill day
            bar = view.bar(ticker, session, adjusted=False)
            if bar is None or not bar.get("open"):
                raise LookupError(f"No opening price for {ticker} on {session}")
            qty = cash / float(bar["open"])
            cash = 0.0

        bar = view.bar(ticker, session, adjusted=False) or view.bar(ticker, adjusted=False)
        if bar is None:
            raise LookupError(f"No price for {ticker} on or before {session}")
        rows.append({
            "session_date": session,
            "equity": cash + qty * float(bar["close"]),
            "cash": cash,
            "qty": qty,
        })

    return pd.DataFrame(rows)


def closed_form_buy_and_hold(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    start: date,
    end: date,
    starting_cash: float,
    *,
    calendar: str = "XNYS",
) -> float:
    """Final equity in one line of arithmetic, for a name with no dividends
    or splits: ``cash × close_last / open_second_session``.

    A third opinion, with no loop in it at all, so a shared mistake between the
    engine and the reference loop still gets caught.
    """
    session_dates = calendar_sessions(start, end, calendar)
    if len(session_dates) < 2:
        raise ValueError("Need at least two sessions for a next-open fill")

    fill_session, last_session = session_dates[1], session_dates[-1]
    open_price = AsOf(conn, fill_session).open_price(ticker, fill_session, adjusted=False)
    last_close = AsOf(conn, last_session).close(ticker, last_session, adjusted=False)
    if open_price is None or last_close is None:
        raise LookupError(f"Missing prices for {ticker}")
    return starting_cash * (last_close / open_price)
