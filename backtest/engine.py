"""The point-in-time backtest engine.

The loop runs one trading session at a time. Within each session the order of
events is fixed, and it is the part worth reading carefully, because getting it
wrong is how a backtest ends up trading on information it did not have:

    1. Apply corporate actions going ex today (splits, dividends).
    2. Fill orders queued yesterday, at today's OPEN.
    3. Mark the portfolio to today's CLOSE and record an equity point.
    4. Hand the strategy a view of the world as of today's close. Anything it
       decides is queued for tomorrow's open — never filled today.

Step 4 is the rule from PRD §7.1, "market-on-open the day after a trigger
fires", and it is also what makes the simulation honest: the strategy can see
today's close because the daily cycle runs after the close, but it cannot trade
on it until the next session.

The strategy never touches the database. It receives an ``AsOf`` bound to the
session date, which physically cannot return data stamped later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Iterable, Protocol, Sequence

import duckdb
import pandas as pd

from store.asof import AsOf

from .metrics import summarise
from .portfolio import Portfolio, Trade
from .trading_calendar import sessions as calendar_sessions


@dataclass
class OrderIntent:
    """What a strategy asks for. Not an order yet — limits still apply."""

    ticker: str
    side: str                       # "buy" | "sell"
    qty: float | None = None        # exact shares
    notional: float | None = None   # or a dollar amount, sized at fill time
    target_pct: float | None = None # or a fraction of equity (§7.1 flat 5%)
    strategy: str = "unknown"
    reason: str = ""
    reference_price: float | None = None
    horizon_end: date | None = None

    def __post_init__(self) -> None:
        if self.side not in ("buy", "sell"):
            raise ValueError(f"side must be 'buy' or 'sell', got {self.side!r}")
        if self.side == "buy" and self.qty is None and self.notional is None and self.target_pct is None:
            raise ValueError(f"buy intent for {self.ticker} specifies no size")


class Strategy(Protocol):
    """What the engine needs from a strategy. Phases 1-3 implement this."""

    name: str

    def on_session(self, view: AsOf, portfolio: Portfolio) -> Sequence[OrderIntent]:
        ...


@dataclass
class ExecutionSettings:
    """Frictions and failure modes.

    Phase 0 runs frictionless on purpose: the exit criterion is that the engine
    reproduces buy-and-hold *exactly*, and any commission or slippage would
    make "exactly" impossible to check. Turn these on in Phase 1, when the
    question changes from "is the engine right" to "does the strategy work".
    """

    commission_per_share: float = 0.0
    slippage_bps: float = 0.0
    allow_fractional_shares: bool = True
    # Cap an order at this share of the session's volume, to simulate the
    # partial fills §8.5 asks about. None means always fill in full.
    max_participation_of_volume: float | None = None
    # How many sessions a queued order keeps retrying through halts before it
    # is abandoned. Real market-on-open orders do not sit around for a week.
    order_ttl_sessions: int = 3


@dataclass
class BacktestResult:
    equity_curve: pd.DataFrame          # session_date, equity, cash, positions_value
    trades: list[Trade]
    portfolio: Portfolio
    metrics: dict
    unfilled: list[dict] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"{key:>22}: {value}" for key, value in self.metrics.items()]
        return "\n".join(lines)


class BacktestEngine:
    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        *,
        starting_cash: float = 100_000.0,
        execution: ExecutionSettings | None = None,
        calendar: str = "XNYS",
        on_event: Callable[[str, dict], None] | None = None,
    ) -> None:
        self.conn = conn
        self.starting_cash = starting_cash
        self.execution = execution or ExecutionSettings()
        self.calendar = calendar
        # Hook for the decision log. Kept as a plain callback so the engine has
        # no opinion about where traces go and stays trivially testable.
        self.on_event = on_event or (lambda kind, payload: None)

    def run(self, strategy: Strategy, start: date, end: date) -> BacktestResult:
        session_dates = calendar_sessions(start, end, self.calendar)
        if not session_dates:
            raise ValueError(f"No trading sessions between {start} and {end}")

        portfolio = Portfolio(cash=self.starting_cash, high_water_mark=self.starting_cash)
        pending: list[tuple[OrderIntent, date, int]] = []   # intent, queued_on, attempts
        unfilled: list[dict] = []
        curve: list[dict] = []
        # Shared across every session's view. Holds unfiltered history only —
        # see AsOf._cached — so it speeds the run up without letting any session
        # see past its own date.
        cache: dict = {}

        for session in session_dates:
            view = AsOf(self.conn, session, cache=cache)

            self._apply_corporate_actions(view, portfolio, session)
            self._liquidate_delisted(view, portfolio, session)
            pending = self._fill_pending(view, portfolio, session, pending, unfilled)
            equity = self._mark_to_market(view, portfolio, session, curve)

            intents = strategy.on_session(view, portfolio) or []
            for intent in intents:
                pending.append((intent, session, 0))
                self.on_event("intent", {
                    "session": session, "ticker": intent.ticker, "side": intent.side,
                    "strategy": intent.strategy, "reason": intent.reason,
                })

        equity_curve = pd.DataFrame(curve)
        return BacktestResult(
            equity_curve=equity_curve,
            trades=portfolio.trades,
            portfolio=portfolio,
            metrics=summarise(equity_curve, portfolio.trades, self.starting_cash),
            unfilled=unfilled,
        )

    # -- session steps -----------------------------------------------------

    def _apply_corporate_actions(self, view: AsOf, portfolio: Portfolio, session: date) -> None:
        for ticker in list(portfolio.positions):
            for action in view.actions_on(ticker, session):
                if action["action_type"] == "split" and action["ratio"]:
                    portfolio.apply_split(ticker, float(action["ratio"]))
                    self.on_event("split", {
                        "session": session, "ticker": ticker, "ratio": float(action["ratio"]),
                    })
                elif action["action_type"] == "cash_dividend" and action["cash_amount"]:
                    amount = portfolio.apply_dividend(ticker, float(action["cash_amount"]))
                    self.on_event("dividend", {
                        "session": session, "ticker": ticker, "amount": amount,
                    })

    def _liquidate_delisted(self, view: AsOf, portfolio: Portfolio, session: date) -> None:
        """Close out anything that has stopped trading.

        Without this, a delisted holding sits in the portfolio for the rest of
        the run marked at its last close — which quietly turns a company that
        disappeared into a position that merely stopped moving, and reintroduces
        the survivorship bias §8.3 exists to prevent.

        The proceeds are the last close. That is right for an acquisition and
        optimistic for a delisting-for-cause, where real recovery is often near
        zero. Closing that gap needs delisting-recovery data the free sources do
        not carry; until then the assumption is written down here rather than
        buried, and it biases results *upward*, so it can never make a strategy
        look worse than it was.
        """
        for ticker in list(portfolio.positions):
            if not view.is_delisted(ticker):
                continue
            bar = view.bar(ticker, adjusted=False)
            if bar is None:
                continue
            price = float(bar["close"])
            qty = portfolio.positions[ticker].qty
            portfolio.sell(ticker, qty, price, session, reason="delisted")
            self.on_event("delisted", {
                "session": session, "ticker": ticker, "qty": qty, "price": price,
            })

    def _fill_pending(
        self,
        view: AsOf,
        portfolio: Portfolio,
        session: date,
        pending: list[tuple[OrderIntent, date, int]],
        unfilled: list[dict],
    ) -> list[tuple[OrderIntent, date, int]]:
        """Fill yesterday's queue at today's open, and carry or drop the rest."""
        still_pending: list[tuple[OrderIntent, date, int]] = []

        for intent, queued_on, attempts in pending:
            bar = view.bar(intent.ticker, session, adjusted=False)

            # No bar means halted, delisted, or a data gap. Retry a few sessions,
            # then give up loudly (§8.5, and §9's "fail closed").
            if bar is None or not bar.get("open"):
                if attempts + 1 >= self.execution.order_ttl_sessions:
                    unfilled.append({
                        "ticker": intent.ticker, "side": intent.side, "queued_on": queued_on,
                        "abandoned_on": session, "reason": "no tradable price within TTL",
                    })
                    self.on_event("order_abandoned", {
                        "session": session, "ticker": intent.ticker, "reason": "no price",
                    })
                else:
                    still_pending.append((intent, queued_on, attempts + 1))
                continue

            fill_price = self._fill_price(float(bar["open"]), intent.side)
            qty = self._resolve_qty(intent, view, portfolio, fill_price, session)
            qty = self._cap_to_volume(qty, bar)

            if qty <= 0:
                unfilled.append({
                    "ticker": intent.ticker, "side": intent.side, "queued_on": queued_on,
                    "abandoned_on": session, "reason": "resolved size was zero",
                })
                continue

            commission = qty * self.execution.commission_per_share
            if intent.side == "buy":
                cost = qty * fill_price + commission
                if cost > portfolio.cash + 1e-9:
                    # Never borrow. Shrink to what the cash actually covers.
                    qty = max(0.0, (portfolio.cash - commission) / fill_price)
                    commission = qty * self.execution.commission_per_share
                if qty <= 0:
                    unfilled.append({
                        "ticker": intent.ticker, "side": "buy", "queued_on": queued_on,
                        "abandoned_on": session, "reason": "insufficient cash",
                    })
                    continue
                portfolio.buy(
                    intent.ticker, qty, fill_price, session,
                    strategy=intent.strategy, commission=commission,
                    reference_price=intent.reference_price, horizon_end=intent.horizon_end,
                )
            else:
                portfolio.sell(
                    intent.ticker, qty, fill_price, session,
                    commission=commission, reason=intent.reason or "strategy exit",
                )

            self.on_event("fill", {
                "session": session, "ticker": intent.ticker, "side": intent.side,
                "qty": qty, "price": fill_price, "strategy": intent.strategy,
                "reason": intent.reason,
            })

        return still_pending

    def _fill_price(self, open_price: float, side: str) -> float:
        if not self.execution.slippage_bps:
            return open_price
        drift = self.execution.slippage_bps / 10_000.0
        return open_price * (1 + drift) if side == "buy" else open_price * (1 - drift)

    def _resolve_qty(
        self,
        intent: OrderIntent,
        view: AsOf,
        portfolio: Portfolio,
        fill_price: float,
        session: date,
    ) -> float:
        if intent.qty is not None:
            qty = intent.qty
        elif intent.notional is not None:
            qty = intent.notional / fill_price
        elif intent.target_pct is not None:
            prices = self._position_prices(view, portfolio, session)
            qty = (portfolio.equity(prices) * intent.target_pct) / fill_price
        elif intent.side == "sell":
            position = portfolio.positions.get(intent.ticker)
            qty = position.qty if position else 0.0
        else:
            qty = 0.0

        if not self.execution.allow_fractional_shares:
            qty = float(int(qty))
        return max(0.0, qty)

    def _cap_to_volume(self, qty: float, bar: dict) -> float:
        cap_pct = self.execution.max_participation_of_volume
        if cap_pct is None or not bar.get("volume"):
            return qty
        return min(qty, float(bar["volume"]) * cap_pct)

    def _position_prices(self, view: AsOf, portfolio: Portfolio, session: date) -> dict[str, float]:
        """Last known close for everything held.

        Falls back to the most recent close before today when a ticker has no
        bar for this session — a halted name still has value, and refusing to
        price it would stop the whole run.
        """
        prices: dict[str, float] = {}
        for ticker in portfolio.positions:
            bar = view.bar(ticker, session, adjusted=False) or view.bar(ticker, adjusted=False)
            if bar is None:
                raise LookupError(
                    f"Holding {ticker} on {session} with no price history at all. "
                    "Refusing to guess a value."
                )
            prices[ticker] = float(bar["close"])
        return prices

    def _mark_to_market(
        self, view: AsOf, portfolio: Portfolio, session: date, curve: list[dict]
    ) -> float:
        prices = self._position_prices(view, portfolio, session)
        equity = portfolio.equity(prices)
        positions_value = equity - portfolio.cash
        drawdown = portfolio.drawdown_from_high(equity)
        curve.append({
            "session_date": session,
            "equity": equity,
            "cash": portfolio.cash,
            "positions_value": positions_value,
            "n_positions": len(portfolio.positions),
            "drawdown": drawdown,
        })
        return equity
