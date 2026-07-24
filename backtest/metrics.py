"""Performance statistics.

PRD §12.2 judges on risk-adjusted return, not raw return, so Sharpe and max
drawdown are first-class here rather than an afterthought. Signal precision —
the share of entries that reach their recovery target — is computed from the
trade list, since that is the number that says whether the *signal* works as
opposed to whether the market went up.
"""

from __future__ import annotations

import math
from typing import Sequence

import pandas as pd

TRADING_DAYS_PER_YEAR = 252


def daily_returns(equity_curve: pd.DataFrame) -> pd.Series:
    if equity_curve.empty:
        return pd.Series(dtype=float)
    return equity_curve["equity"].pct_change().dropna()


def sharpe(equity_curve: pd.DataFrame, risk_free_rate: float = 0.0) -> float | None:
    """Annualised Sharpe on daily returns.

    ``risk_free_rate`` is an annual figure. Returns ``None`` rather than a
    divide-by-zero when the equity curve never moves, which is what a backtest
    that took no trades looks like.
    """
    returns = daily_returns(equity_curve)
    if len(returns) < 2:
        return None
    excess = returns - (risk_free_rate / TRADING_DAYS_PER_YEAR)
    std = excess.std(ddof=1)
    if not std or math.isclose(std, 0.0):
        return None
    return float((excess.mean() / std) * math.sqrt(TRADING_DAYS_PER_YEAR))


def max_drawdown(equity_curve: pd.DataFrame) -> float:
    """Worst peak-to-trough fall, as a negative fraction."""
    if equity_curve.empty:
        return 0.0
    equity = equity_curve["equity"]
    return float((equity / equity.cummax() - 1.0).min())


def annualised_return(equity_curve: pd.DataFrame, starting_cash: float) -> float | None:
    if len(equity_curve) < 2:
        return None
    start = equity_curve["session_date"].iloc[0]
    end = equity_curve["session_date"].iloc[-1]
    years = (pd.Timestamp(end) - pd.Timestamp(start)).days / 365.25
    if years <= 0:
        return None
    final = float(equity_curve["equity"].iloc[-1])
    return float((final / starting_cash) ** (1 / years) - 1.0)


def volatility(equity_curve: pd.DataFrame) -> float | None:
    returns = daily_returns(equity_curve)
    if len(returns) < 2:
        return None
    return float(returns.std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR))


def trade_stats(trades: Sequence) -> dict:
    """Per-trade statistics, split by originating strategy (§7.1, §12.2).

    The split is the point: the whole project exists to find out whether the
    LLM layer and Strategy B each add anything, and that is unanswerable from a
    single blended number.
    """
    if not trades:
        return {"n_trades": 0, "win_rate": None, "avg_return": None, "by_strategy": {}}

    returns = [t.return_pct for t in trades]
    wins = [r for r in returns if r > 0]

    by_strategy: dict[str, dict] = {}
    for trade in trades:
        bucket = by_strategy.setdefault(trade.strategy, {"n": 0, "returns": []})
        bucket["n"] += 1
        bucket["returns"].append(trade.return_pct)
    for name, bucket in by_strategy.items():
        rets = bucket.pop("returns")
        bucket["win_rate"] = sum(1 for r in rets if r > 0) / len(rets)
        bucket["avg_return"] = sum(rets) / len(rets)

    return {
        "n_trades": len(trades),
        "win_rate": len(wins) / len(returns),
        "avg_return": sum(returns) / len(returns),
        "avg_days_held": sum(t.days_held for t in trades) / len(trades),
        "by_strategy": by_strategy,
    }


def summarise(equity_curve: pd.DataFrame, trades: Sequence, starting_cash: float) -> dict:
    if equity_curve.empty:
        return {"error": "no equity curve produced"}
    final_equity = float(equity_curve["equity"].iloc[-1])
    return {
        "start": equity_curve["session_date"].iloc[0],
        "end": equity_curve["session_date"].iloc[-1],
        "sessions": len(equity_curve),
        "starting_equity": starting_cash,
        "final_equity": final_equity,
        "total_return": final_equity / starting_cash - 1.0,
        "annualised_return": annualised_return(equity_curve, starting_cash),
        "volatility": volatility(equity_curve),
        "sharpe": sharpe(equity_curve),
        "max_drawdown": max_drawdown(equity_curve),
        **trade_stats(trades),
    }
