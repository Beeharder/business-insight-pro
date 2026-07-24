"""The universe screen (PRD §4.1).

Size and liquidity thresholds, applied point-in-time. A company that is worth
$40bn today may have been worth $6bn in 2019, and a backtest that lets it into
the 2019 universe is using knowledge from 2024.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from store import metrics as M
from store.asof import AsOf

from .survivability import Check


@dataclass
class UniverseResult:
    ticker: str
    as_of: date
    passed: bool
    checks: list[Check]

    @property
    def failure_summary(self) -> str:
        return "; ".join(f"{c.name}: {c.reason}" for c in self.checks if not c.passed)


def market_cap(view: AsOf, ticker: str) -> float | None:
    """Market cap as it stood on the as-of date.

    Prefers a reported figure; otherwise shares outstanding from the last filing
    multiplied by that day's close. Both inputs are point-in-time, so an old
    share count is used until a newer filing supersedes it — which is what a
    reader actually had.
    """
    reported = view.fundamental(ticker, M.MARKET_CAP)
    if reported is not None:
        return reported
    shares = view.fundamental(ticker, M.SHARES_OUTSTANDING)
    price = view.close(ticker)
    if shares is None or price is None:
        return None
    return shares * price


def universe_screen(
    view: AsOf,
    ticker: str,
    *,
    min_market_cap: float,
    min_avg_dollar_volume: float,
    adv_window_days: int = 30,
    min_years_since_ipo: int = 2,
    exclude_adr: bool = True,
    exclude_spac: bool = True,
    allowed_security_types: tuple[str, ...] = ("common_stock",),
) -> UniverseResult:
    checks: list[Check] = []
    security = view.security(ticker)

    if security is None:
        checks.append(Check("known_security", False, "ticker not in the securities table"))
        return UniverseResult(ticker, view.as_of, False, checks)

    security_type = (security.get("security_type") or "").lower()
    type_ok = security_type in allowed_security_types
    if exclude_adr and security_type == "adr":
        type_ok = False
    if exclude_spac and security_type == "spac":
        type_ok = False
    checks.append(Check("security_type", type_ok,
                        f"security type is {security_type or 'unknown'}"))

    ipo = security.get("ipo_date") or security.get("first_trade_date")
    if ipo is None:
        checks.append(Check("seasoning", False, "no IPO or first-trade date on record"))
    else:
        import pandas as pd

        ipo_date = pd.Timestamp(ipo).date()
        years = (view.as_of - ipo_date).days / 365.25
        checks.append(Check("seasoning", years >= min_years_since_ipo,
                            f"{years:.1f} years public against a {min_years_since_ipo}-year minimum",
                            {"ipo_date": ipo_date}))

    cap = market_cap(view, ticker)
    if cap is None:
        checks.append(Check("market_cap", False, "market cap not computable on this date"))
    else:
        checks.append(Check("market_cap", cap >= min_market_cap,
                            f"market cap {cap:,.0f} against a {min_market_cap:,.0f} minimum",
                            {"market_cap": cap}))

    adv = view.avg_dollar_volume(ticker, adv_window_days)
    if adv is None:
        checks.append(Check("liquidity", False,
                            f"fewer than {adv_window_days} sessions of price history"))
    else:
        checks.append(Check("liquidity", adv >= min_avg_dollar_volume,
                            f"average dollar volume {adv:,.0f} against a {min_avg_dollar_volume:,.0f} minimum",
                            {"avg_dollar_volume": adv}))

    return UniverseResult(ticker, view.as_of, all(c.passed for c in checks), checks)
