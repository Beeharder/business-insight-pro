"""Backtest engine tests.

The headline test is the Phase 0 exit criterion from PRD §11 — the engine must
reproduce buy-and-hold *exactly*. The rest check the mechanics that criterion
depends on: when orders fill, how splits and dividends move through a position,
and whether a delisting is handled or quietly ignored.
"""

from __future__ import annotations

from datetime import date, datetime, time

import pytest

from backtest import (
    BacktestEngine,
    BuyAndHold,
    ExecutionSettings,
    OrderIntent,
    closed_form_buy_and_hold,
    reference_buy_and_hold,
    sessions,
)

START, END = date(2021, 1, 4), date(2023, 12, 29)
CASH = 100_000.0


# -- the Phase 0 exit criterion ----------------------------------------------


@pytest.mark.parametrize("ticker", ["SPY", "SPLITCO", "BIGCO", "GONECO"])
def test_engine_reproduces_buy_and_hold_exactly(demo_conn, ticker):
    """Every session of the equity curve must match an independent calculation.

    Exact equality, not "close enough". A tolerance here would hide precisely
    the errors this test exists to catch: an off-by-one in fill timing shifts
    the curve by a day, which a loose tolerance would wave through.
    """
    result = BacktestEngine(demo_conn, starting_cash=CASH).run(BuyAndHold(ticker), START, END)
    reference = reference_buy_and_hold(demo_conn, ticker, START, END, CASH)

    assert len(result.equity_curve) == len(reference)
    gaps = (result.equity_curve["equity"] - reference["equity"]).abs()
    assert gaps.max() == 0.0


def test_engine_matches_closed_form_for_a_clean_name(demo_conn):
    """SPY has no splits or dividends in the synthetic world, so the answer is
    one line of arithmetic — a check that shares no code with either the engine
    or the reference loop."""
    result = BacktestEngine(demo_conn, starting_cash=CASH).run(BuyAndHold("SPY"), START, END)
    expected = closed_form_buy_and_hold(demo_conn, "SPY", START, END, CASH)
    assert float(result.equity_curve["equity"].iloc[-1]) == expected


# -- fill timing -------------------------------------------------------------


class OrderOnce:
    """Places one order on a named session and nothing else."""

    name = "order_once"

    def __init__(self, ticker: str, on: date, **kwargs):
        self.ticker, self.on, self.kwargs = ticker, on, kwargs

    def on_session(self, view, portfolio):
        if view.as_of != self.on:
            return []
        return [OrderIntent(ticker=self.ticker, side="buy", strategy=self.name, **self.kwargs)]


def test_order_fills_at_the_next_sessions_open(demo_conn):
    """§7.1: market-on-open the day after a trigger fires.

    Filling on the trigger day would let the strategy trade on a close it had
    only just seen — a full day of free lookahead on every position.
    """
    trigger = date(2022, 3, 15)
    fill_day = sessions(trigger, date(2022, 3, 22))[1]

    fills = []
    engine = BacktestEngine(
        demo_conn, starting_cash=CASH,
        on_event=lambda kind, payload: fills.append(payload) if kind == "fill" else None,
    )
    engine.run(OrderOnce("BIGCO", trigger, notional=10_000.0), START, END)

    assert len(fills) == 1
    assert fills[0]["session"] == fill_day

    from store import AsOf
    expected_open = AsOf(demo_conn, fill_day).open_price("BIGCO", fill_day, adjusted=False)
    assert fills[0]["price"] == expected_open


def test_no_position_exists_on_the_trigger_day(demo_conn):
    trigger = date(2022, 3, 15)

    seen = {}

    class Watcher(OrderOnce):
        def on_session(self, view, portfolio):
            if view.as_of == self.on:
                seen["at_trigger"] = len(portfolio.positions)
            return super().on_session(view, portfolio)

    BacktestEngine(demo_conn, starting_cash=CASH).run(
        Watcher("BIGCO", trigger, notional=10_000.0), START, END
    )
    assert seen["at_trigger"] == 0


# -- sizing ------------------------------------------------------------------

def test_target_pct_sizes_against_portfolio_equity(demo_conn):
    """§7.1's flat 5% sizing."""
    fills = []
    BacktestEngine(
        demo_conn, starting_cash=CASH,
        on_event=lambda kind, payload: fills.append(payload) if kind == "fill" else None,
    ).run(OrderOnce("BIGCO", date(2022, 3, 15), target_pct=0.05), START, END)

    assert len(fills) == 1
    assert fills[0]["qty"] * fills[0]["price"] == pytest.approx(CASH * 0.05, rel=1e-9)


def test_engine_never_borrows(demo_conn):
    """An order for more than the cash available is trimmed, not funded."""
    result = BacktestEngine(demo_conn, starting_cash=1_000.0).run(
        OrderOnce("BIGCO", date(2022, 3, 15), notional=50_000.0), START, END
    )
    assert result.portfolio.cash >= -1e-9
    assert result.equity_curve["cash"].min() >= -1e-9


def test_buy_intent_without_a_size_is_rejected_immediately(demo_conn):
    """Caught when the intent is constructed, not silently sized to zero."""
    with pytest.raises(ValueError):
        OrderIntent(ticker="BIGCO", side="buy")


def test_invalid_side_is_rejected(demo_conn):
    with pytest.raises(ValueError):
        OrderIntent(ticker="BIGCO", side="short", qty=1)


# -- corporate actions -------------------------------------------------------


def test_split_leaves_portfolio_value_continuous(demo_conn):
    """A 2-for-1 split doubles the shares and halves the price.

    If only one side of that were applied, equity would jump or halve overnight
    — and in a strategy with an -18% stop (§7.1), a phantom halving fires the
    stop and closes a position that never moved.
    """
    result = BacktestEngine(demo_conn, starting_cash=CASH).run(BuyAndHold("SPLITCO"), START, END)
    curve = result.equity_curve.set_index("session_date")["equity"]
    daily_change = curve.pct_change().abs().dropna()
    assert daily_change.max() < 0.25


def test_dividends_are_credited_to_cash(demo_conn):
    result = BacktestEngine(demo_conn, starting_cash=CASH).run(BuyAndHold("BIGCO"), START, END)
    assert result.portfolio.dividends_received > 0
    assert result.portfolio.cash == pytest.approx(result.portfolio.dividends_received, rel=1e-9)


def test_delisted_holding_is_closed_out(demo_conn):
    """Otherwise a dead position sits at a stale price forever, which is
    survivorship bias sneaking back in through the portfolio (§8.3)."""
    result = BacktestEngine(demo_conn, starting_cash=CASH).run(BuyAndHold("GONECO"), START, END)
    assert any(trade.exit_reason == "delisted" for trade in result.trades)
    assert "GONECO" not in result.portfolio.positions
    assert result.equity_curve["positions_value"].iloc[-1] == 0.0


# -- frictions ---------------------------------------------------------------


def test_slippage_and_commission_reduce_returns(demo_conn):
    frictionless = BacktestEngine(demo_conn, starting_cash=CASH).run(
        BuyAndHold("SPY"), START, END
    )
    with_costs = BacktestEngine(
        demo_conn, starting_cash=CASH,
        execution=ExecutionSettings(commission_per_share=0.005, slippage_bps=5.0),
    ).run(BuyAndHold("SPY"), START, END)

    assert with_costs.metrics["final_equity"] < frictionless.metrics["final_equity"]


def test_metrics_are_computed(demo_conn):
    result = BacktestEngine(demo_conn, starting_cash=CASH).run(BuyAndHold("SPY"), START, END)
    assert result.metrics["sessions"] == len(result.equity_curve)
    assert result.metrics["max_drawdown"] <= 0.0
    assert result.metrics["sharpe"] is not None
