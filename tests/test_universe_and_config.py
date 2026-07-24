"""Universe screen (§4.1) and configuration tests.

The universe thresholds exist because of a specific loss: §4.1 records that the
owner's one total loss was a small-cap that delisted. These tests check the
thresholds are actually enforced, and — like the survivability tests — that
missing data rejects rather than passes.
"""

from __future__ import annotations

from datetime import date, datetime, time

import pytest

from config import load_config
from config.loader import MissingSecret, require_secret
from screens import universe_screen
from store import AsOf
from store import metrics as M

CLOSE = time(16, 0)
BIG = 10_000_000_000.0
LIQUID = 50_000_000.0


def _listed(conn, ticker, *, security_type="common_stock", ipo=date(2015, 1, 2)):
    conn.execute(
        """INSERT INTO securities (ticker, name, security_type, ipo_date,
                                   first_trade_date, available_at, source)
           VALUES (?,?,?,?,?,?,?)""",
        [ticker, ticker, security_type, ipo, ipo, datetime.combine(ipo, CLOSE), "test"],
    )


def _price_history(conn, ticker, *, close_price, volume, sessions=40,
                   end=date(2023, 6, 30)):
    from backtest.trading_calendar import sessions as calendar_sessions

    days = calendar_sessions(date(2023, 1, 3), end)[-sessions:]
    for session in days:
        conn.execute(
            """INSERT INTO bars (ticker, session_date, open, high, low, close,
                                 volume, is_halted, available_at, source)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            [ticker, session, close_price, close_price, close_price, close_price,
             volume, False, datetime.combine(session, CLOSE), "test"],
        )


def _shares(conn, ticker, count, *, filed=date(2023, 2, 1)):
    conn.execute(
        """INSERT INTO fundamentals (ticker, period_end, fiscal_period, metric, value,
                                     filing_date, available_at, source)
           VALUES (?,?,?,?,?,?,?,?)""",
        [ticker, date(2022, 12, 31), "Q4", M.SHARES_OUTSTANDING, count, filed,
         datetime.combine(filed, CLOSE), "test"],
    )


def _screen(conn, ticker, as_of=date(2023, 6, 30)):
    return universe_screen(
        AsOf(conn, as_of), ticker,
        min_market_cap=BIG, min_avg_dollar_volume=LIQUID, adv_window_days=30,
    )


def _failed(result):
    return {check.name for check in result.checks if not check.passed}


# -- the thresholds ----------------------------------------------------------


def test_a_large_liquid_name_passes(conn):
    _listed(conn, "BIG")
    _price_history(conn, "BIG", close_price=100.0, volume=2_000_000)   # $200M/day
    _shares(conn, "BIG", 500_000_000)                                  # $50bn
    assert _screen(conn, "BIG").passed is True


def test_a_small_cap_is_excluded(conn):
    _listed(conn, "SMALL")
    _price_history(conn, "SMALL", close_price=100.0, volume=2_000_000)
    _shares(conn, "SMALL", 20_000_000)                                 # $2bn
    assert "market_cap" in _failed(_screen(conn, "SMALL"))


def test_an_illiquid_name_is_excluded(conn):
    """§4.1's liquidity floor. A position you cannot exit is not a position."""
    _listed(conn, "THIN")
    _price_history(conn, "THIN", close_price=100.0, volume=50_000)      # $5M/day
    _shares(conn, "THIN", 500_000_000)
    assert "liquidity" in _failed(_screen(conn, "THIN"))


def test_a_recent_ipo_is_excluded(conn):
    _listed(conn, "NEW", ipo=date(2022, 6, 1))
    _price_history(conn, "NEW", close_price=100.0, volume=2_000_000)
    _shares(conn, "NEW", 500_000_000)
    assert "seasoning" in _failed(_screen(conn, "NEW"))


@pytest.mark.parametrize("security_type", ["adr", "spac", "etf"])
def test_excluded_security_types_are_rejected(conn, security_type):
    _listed(conn, "EXCL", security_type=security_type)
    _price_history(conn, "EXCL", close_price=100.0, volume=2_000_000)
    _shares(conn, "EXCL", 500_000_000)
    assert "security_type" in _failed(_screen(conn, "EXCL"))


# -- point-in-time behaviour -------------------------------------------------


def test_market_cap_uses_the_price_on_the_as_of_date(conn):
    """A company worth $40bn today may have been worth $6bn in 2019.

    Screening 2019 with today's market cap is using knowledge from the future,
    and it systematically admits the winners.
    """
    from backtest.trading_calendar import sessions as calendar_sessions

    _listed(conn, "GROWTH")
    _shares(conn, "GROWTH", 500_000_000, filed=date(2019, 2, 1))
    for session in calendar_sessions(date(2019, 1, 2), date(2023, 6, 30)):
        price = 10.0 if session.year <= 2019 else 100.0
        conn.execute(
            """INSERT INTO bars (ticker, session_date, open, high, low, close,
                                 volume, is_halted, available_at, source)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ["GROWTH", session, price, price, price, price,
             2_000_000, False, datetime.combine(session, CLOSE), "test"],
        )

    assert "market_cap" in _failed(_screen(conn, "GROWTH", as_of=date(2019, 6, 28)))
    assert _screen(conn, "GROWTH", as_of=date(2023, 6, 30)).passed is True


def test_share_count_is_not_known_before_it_is_filed(conn):
    _listed(conn, "LATE")
    _price_history(conn, "LATE", close_price=100.0, volume=2_000_000)
    _shares(conn, "LATE", 500_000_000, filed=date(2023, 6, 1))

    assert "market_cap" in _failed(_screen(conn, "LATE", as_of=date(2023, 5, 1)))
    assert "market_cap" not in _failed(_screen(conn, "LATE", as_of=date(2023, 6, 30)))


# -- fail closed -------------------------------------------------------------


def test_an_unknown_ticker_is_rejected(conn):
    result = _screen(conn, "NOSUCH")
    assert result.passed is False
    assert "known_security" in _failed(result)


def test_missing_share_count_rejects_rather_than_assumes(conn):
    _listed(conn, "NOSHARES")
    _price_history(conn, "NOSHARES", close_price=100.0, volume=2_000_000)
    assert "market_cap" in _failed(_screen(conn, "NOSHARES"))


def test_too_little_price_history_rejects(conn):
    _listed(conn, "SHORT")
    _price_history(conn, "SHORT", close_price=100.0, volume=2_000_000, sessions=5)
    _shares(conn, "SHORT", 500_000_000)
    assert "liquidity" in _failed(_screen(conn, "SHORT"))


# -- configuration -----------------------------------------------------------


def test_config_matches_the_prd_thresholds():
    """Guards against a threshold being changed by accident.

    §13 says loosen only on backtest evidence. A test that pins the PRD values
    turns a silent edit into a failing build.
    """
    cfg = load_config()
    assert cfg.universe.min_market_cap_usd == 10_000_000_000
    assert cfg.universe.min_avg_dollar_volume_usd == 50_000_000
    assert cfg.survivability.min_runway_months == 18
    assert cfg.strategy_a.shock.price_decline_pct == 0.12
    assert cfg.strategy_a.shock.sector_relative_underperf_pp == 0.08
    assert cfg.strategy_a.shock.news_volume_multiple == 3.0
    assert cfg.strategy_a.decay.article_volume_drop_from_peak == 0.60
    assert cfg.strategy_a.decay.watch_expiry_days == 30
    assert cfg.strategy_a.llm.min_confidence == 0.70
    assert cfg.limits.max_concurrent_positions == 8
    assert cfg.limits.max_sector_exposure_pct == 0.25
    assert cfg.limits.max_new_positions_per_day == 2
    assert cfg.limits.portfolio_drawdown_kill_switch_pct == -0.10
    assert cfg.positions.exit.stop_loss_pct == -0.18
    assert cfg.regime.vix_ceiling == 30
    assert cfg.regime.spy_below_sma_days == 200
    assert cfg.regime.clear_sessions_required == 5


def test_a_missing_setting_names_itself(conn):
    cfg = load_config()
    with pytest.raises(AttributeError, match="no_such_setting"):
        cfg.no_such_setting


def test_a_missing_secret_explains_the_fix(monkeypatch):
    """§9: the owner is not a developer. An error has to say what to do."""
    monkeypatch.delenv("SOME_KEY", raising=False)
    with pytest.raises(MissingSecret) as excinfo:
        require_secret("SOME_KEY", "do the thing")
    message = str(excinfo.value)
    assert ".env" in message
    assert "do the thing" in message


def test_backtest_defaults_are_frictionless():
    """Phase 0 measures engine correctness against buy-and-hold, which only
    works if nothing eats into returns. Phase 1 turns these on."""
    cfg = load_config()
    assert cfg.backtest.commission_per_share_usd == 0.0
    assert cfg.backtest.slippage_bps == 0.0
