"""Point-in-time backtesting (PRD Phase 0)."""

from .benchmark import BuyAndHold, closed_form_buy_and_hold, reference_buy_and_hold
from .engine import BacktestEngine, BacktestResult, ExecutionSettings, OrderIntent, Strategy
from .portfolio import Portfolio, Position, Trade
from .trading_calendar import next_session, previous_session, sessions

__all__ = [
    "BacktestEngine",
    "BacktestResult",
    "ExecutionSettings",
    "OrderIntent",
    "Strategy",
    "Portfolio",
    "Position",
    "Trade",
    "BuyAndHold",
    "reference_buy_and_hold",
    "closed_form_buy_and_hold",
    "sessions",
    "next_session",
    "previous_session",
]
