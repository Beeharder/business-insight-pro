"""Positions, cash, and the arithmetic of holding things.

Share counts are floats. Alpaca supports fractional shares, and using them
keeps the flat 5% sizing of §7.1 exact instead of rounding it to whole shares
and quietly drifting off the intended weight.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Mapping


@dataclass
class Position:
    ticker: str
    qty: float
    avg_price: float
    entry_date: date
    strategy: str = "unknown"          # §7.1: performance must be attributable
    reference_price: float | None = None  # pre-shock price, for the §7.1 A exit
    horizon_end: date | None = None    # time stop, set from the §5.4 taxonomy

    @property
    def cost_basis(self) -> float:
        return self.qty * self.avg_price

    def market_value(self, price: float) -> float:
        return self.qty * price

    def unrealised_pct(self, price: float) -> float:
        return (price / self.avg_price) - 1.0 if self.avg_price else 0.0


@dataclass
class Trade:
    """A completed round trip, which is the unit §12.2 measures precision over."""

    ticker: str
    strategy: str
    entry_date: date
    exit_date: date
    entry_price: float
    exit_price: float
    qty: float
    exit_reason: str

    @property
    def return_pct(self) -> float:
        return (self.exit_price / self.entry_price) - 1.0

    @property
    def pnl(self) -> float:
        return (self.exit_price - self.entry_price) * self.qty

    @property
    def days_held(self) -> int:
        return (self.exit_date - self.entry_date).days


@dataclass
class Portfolio:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    trades: list[Trade] = field(default_factory=list)
    realised_pnl: float = 0.0
    dividends_received: float = 0.0
    commission_paid: float = 0.0
    high_water_mark: float = 0.0

    def equity(self, prices: Mapping[str, float]) -> float:
        """Cash plus marked positions.

        A position whose ticker is missing from ``prices`` is a bug in the
        caller, not something to skip — skipping it would silently value the
        position at zero and make a data outage look like a loss.
        """
        total = self.cash
        for ticker, position in self.positions.items():
            if ticker not in prices:
                raise KeyError(
                    f"No price for held position {ticker}; refusing to value the "
                    "portfolio with a gap in it."
                )
            total += position.market_value(prices[ticker])
        return total

    def drawdown_from_high(self, equity: float) -> float:
        """Current drawdown as a negative fraction — the §7.2 kill-switch input."""
        self.high_water_mark = max(self.high_water_mark, equity)
        if self.high_water_mark <= 0:
            return 0.0
        return (equity / self.high_water_mark) - 1.0

    # -- mutations ---------------------------------------------------------

    def buy(
        self,
        ticker: str,
        qty: float,
        price: float,
        when: date,
        *,
        strategy: str = "unknown",
        commission: float = 0.0,
        reference_price: float | None = None,
        horizon_end: date | None = None,
    ) -> None:
        self.cash -= qty * price + commission
        self.commission_paid += commission
        existing = self.positions.get(ticker)
        if existing:
            total_qty = existing.qty + qty
            existing.avg_price = (existing.cost_basis + qty * price) / total_qty
            existing.qty = total_qty
        else:
            self.positions[ticker] = Position(
                ticker=ticker, qty=qty, avg_price=price, entry_date=when,
                strategy=strategy, reference_price=reference_price,
                horizon_end=horizon_end,
            )

    def sell(
        self,
        ticker: str,
        qty: float,
        price: float,
        when: date,
        *,
        commission: float = 0.0,
        reason: str = "unspecified",
    ) -> Trade | None:
        position = self.positions.get(ticker)
        if position is None:
            return None
        qty = min(qty, position.qty)
        self.cash += qty * price - commission
        self.commission_paid += commission
        self.realised_pnl += (price - position.avg_price) * qty

        trade = Trade(
            ticker=ticker, strategy=position.strategy, entry_date=position.entry_date,
            exit_date=when, entry_price=position.avg_price, exit_price=price,
            qty=qty, exit_reason=reason,
        )
        self.trades.append(trade)

        position.qty -= qty
        if position.qty <= 1e-12:
            del self.positions[ticker]
        return trade

    def apply_split(self, ticker: str, ratio: float) -> None:
        """A 2-for-1 split doubles the shares and halves the price.

        Applied to the share count only; prices come from the store unadjusted,
        so the two stay consistent and portfolio value is continuous through the
        split rather than jumping by the ratio.
        """
        position = self.positions.get(ticker)
        if position:
            position.qty *= ratio
            position.avg_price /= ratio
            if position.reference_price is not None:
                position.reference_price /= ratio

    def apply_dividend(self, ticker: str, cash_per_share: float) -> float:
        """Credit a cash dividend on its ex-date.

        Real cash arrives a few weeks after the ex-date. Crediting it on the
        ex-date is the standard simplification and is applied identically to the
        benchmark, so comparisons stay honest.
        """
        position = self.positions.get(ticker)
        if not position:
            return 0.0
        amount = position.qty * cash_per_share
        self.cash += amount
        self.dividends_received += amount
        return amount
