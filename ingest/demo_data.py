"""Deterministic synthetic data, for tests and for the offline demo.

This is **not** market data and must never be mistaken for it. It exists so that
the engine, the point-in-time reader and the survivability gate can be exercised
end to end before any API key is in place, and so the test suite runs the same
way on every machine and in CI.

Everything here comes from a fixed seed, so two runs produce identical numbers
and a failing test always fails the same way.

The one case drawn from reality is EXRO, which PRD §8.3 requires as a test the
survivability gate must reject. Its figures below are illustrative rather than
audited — what is being tested is that the gate rejects a company with these
characteristics, not the precise contents of any filing.
"""

from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta

import duckdb

from backtest.trading_calendar import sessions as calendar_sessions
from store import metrics as M

CLOSE_TIME = time(16, 0)


def _walk(seed: int, n: int, start_price: float, drift: float, vol: float) -> list[float]:
    """A reproducible price path. Not a model of anything — just numbers that
    move like prices do, so the plumbing gets exercised realistically."""
    rng = random.Random(seed)
    prices, price = [], start_price
    for _ in range(n):
        price *= 1.0 + rng.gauss(drift, vol)
        price = max(price, 0.01)
        prices.append(round(price, 4))
    return prices


def _insert_bars(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    session_dates: list[date],
    closes: list[float],
    *,
    volume: int = 5_000_000,
    source: str = "synthetic",
    opens: list[float] | None = None,
) -> None:
    """Write bars, deriving each open from the previous close unless given.

    ``opens`` matters on a split ex-date, where the open is already the
    post-split price and does not follow from the previous session's close.
    """
    rows = []
    previous_close = closes[0]
    for index, (session, close) in enumerate(zip(session_dates, closes)):
        open_price = (round(opens[index], 4) if opens is not None
                      else round(previous_close * 1.001, 4))
        high = round(max(open_price, close) * 1.004, 4)
        low = round(min(open_price, close) * 0.996, 4)
        rows.append((
            ticker, session, open_price, high, low, close, volume, 1000,
            round((high + low + close) / 3, 4), False,
            datetime.combine(session, CLOSE_TIME), source,
        ))
        previous_close = close
    conn.executemany(
        """INSERT OR REPLACE INTO bars
           (ticker, session_date, open, high, low, close, volume, trade_count,
            vwap, is_halted, available_at, source)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )


def _insert_security(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    *,
    name: str,
    sector: str = "Technology",
    sector_etf: str = "XLK",
    security_type: str = "common_stock",
    first_trade: date,
    known_from: date,
    ipo_date: date | None = None,
    last_trade: date | None = None,
    delisted: date | None = None,
    delisting_reason: str | None = None,
    delisting_notice_date: date | None = None,
    exchange: str = "NASDAQ",
) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO securities
           (ticker, cik, name, exchange, sector, sector_etf, security_type, ipo_date,
            first_trade_date, last_trade_date, delisted_date, delisting_reason,
            delisting_notice_date, available_at, source)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            ticker, f"CIK{abs(hash(ticker)) % 10**7:07d}", name, exchange, sector,
            sector_etf, security_type, ipo_date or first_trade, first_trade,
            last_trade, delisted, delisting_reason, delisting_notice_date,
            datetime.combine(known_from, CLOSE_TIME), "synthetic",
        ],
    )


def _insert_quarters(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    metric: str,
    quarters: list[tuple[date, str, float]],
    *,
    filing_lag_days: int = 40,
    unit: str = "USD",
) -> None:
    """Insert quarterly figures, each stamped with a realistic filing lag.

    The lag is the whole point: a quarter ending 31 March does not become
    knowable until the 10-Q lands in May (§8.3).
    """
    rows = []
    for period_end, fiscal_period, value in quarters:
        filing_date = period_end + timedelta(days=filing_lag_days)
        rows.append((
            None, ticker, period_end, fiscal_period, metric, value, unit,
            filing_date, f"{ticker}-{period_end:%Y%m%d}", False,
            datetime.combine(filing_date, CLOSE_TIME), "synthetic",
        ))
    conn.executemany(
        """INSERT OR REPLACE INTO fundamentals
           (cik, ticker, period_end, fiscal_period, metric, value, unit,
            filing_date, accession, is_restatement, available_at, source)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )


def _quarter_ends(start_year: int, n: int) -> list[tuple[date, str]]:
    ends = [(3, 31, "Q1"), (6, 30, "Q2"), (9, 30, "Q3"), (12, 31, "Q4")]
    out = []
    year = start_year
    while len(out) < n:
        for month, day, label in ends:
            out.append((date(year, month, day), label))
            if len(out) == n:
                break
        year += 1
    return out


def seed_demo(
    conn: duckdb.DuckDBPyConnection,
    *,
    start: date = date(2021, 1, 4),
    end: date = date(2023, 12, 29),
) -> dict[str, int]:
    """Populate an empty store with a small synthetic world.

    Contains, on purpose:

    * ``SPY`` — the benchmark, no corporate actions, so buy-and-hold has a
      closed-form answer to check the engine against.
    * ``BIGCO`` — healthy large cap that passes the §4.2 gate; pays dividends.
    * ``SPLITCO`` — has a 2-for-1 split, so split handling is exercised.
    * ``FADECO`` — sells off hard and recovers; the shape Strategy A looks for.
    * ``EXRO`` — the §8.3 required rejection case: going concern, negative cash
      flow, delisting notice.
    * ``GONECO`` — delists mid-sample, so survivorship handling is exercised.
    """
    session_dates = calendar_sessions(start, end)
    n = len(session_dates)

    # --- benchmark ---------------------------------------------------------
    spy_closes = _walk(seed=11, n=n, start_price=370.0, drift=0.0004, vol=0.009)
    _insert_security(conn, "SPY", name="SPDR S&P 500 ETF", sector="Index",
                     sector_etf="SPY", security_type="etf",
                     first_trade=date(1993, 1, 29), known_from=start,
                     exchange="NYSE")
    _insert_bars(conn, "SPY", session_dates, spy_closes)
    conn.executemany(
        """INSERT OR REPLACE INTO index_levels (symbol, session_date, close, available_at, source)
           VALUES (?,?,?,?,?)""",
        [("SPY", s, c, datetime.combine(s, CLOSE_TIME), "synthetic")
         for s, c in zip(session_dates, spy_closes)],
    )
    vix_closes = _walk(seed=12, n=n, start_price=18.0, drift=0.0, vol=0.05)
    conn.executemany(
        """INSERT OR REPLACE INTO index_levels (symbol, session_date, close, available_at, source)
           VALUES (?,?,?,?,?)""",
        [("VIX", s, c, datetime.combine(s, CLOSE_TIME), "synthetic")
         for s, c in zip(session_dates, vix_closes)],
    )

    # --- a healthy large cap ----------------------------------------------
    _insert_security(conn, "BIGCO", name="Bigco Industries", sector="Industrials",
                     sector_etf="XLI", first_trade=date(2005, 5, 2), known_from=start)
    _insert_bars(conn, "BIGCO", session_dates, _walk(21, n, 140.0, 0.0005, 0.014))
    quarters = _quarter_ends(2020, 16)
    _insert_quarters(conn, "BIGCO", M.OPERATING_CASH_FLOW,
                     [(pe, fp, 900_000_000.0) for pe, fp in quarters])
    _insert_quarters(conn, "BIGCO", M.CAPEX,
                     [(pe, fp, 300_000_000.0) for pe, fp in quarters])
    _insert_quarters(conn, "BIGCO", M.CASH_AND_EQUIVALENTS,
                     [(pe, fp, 8_000_000_000.0) for pe, fp in quarters])
    _insert_quarters(conn, "BIGCO", M.DEBT_DUE_WITHIN_24M,
                     [(pe, fp, 1_500_000_000.0) for pe, fp in quarters])
    _insert_quarters(conn, "BIGCO", M.REVENUE,
                     [(pe, fp, 6_000_000_000.0) for pe, fp in quarters])
    # A dividend every quarter, ex-date shortly after each quarter end.
    conn.executemany(
        """INSERT OR REPLACE INTO corporate_actions
           (ticker, ex_date, action_type, ratio, cash_amount, announced_at, available_at, source)
           VALUES (?,?,?,?,?,?,?,?)""",
        [
            ("BIGCO", ex, "cash_dividend", None, 0.85,
             datetime.combine(ex - timedelta(days=20), CLOSE_TIME),
             datetime.combine(ex - timedelta(days=20), CLOSE_TIME), "synthetic")
            for ex in [d for d in session_dates if d.month in (2, 5, 8, 11) and d.day in (14, 15, 16)]
        ],
    )
    _insert_10k(conn, "BIGCO", filing_date=date(2022, 2, 18), period_end=date(2021, 12, 31))
    _insert_10k(conn, "BIGCO", filing_date=date(2023, 2, 17), period_end=date(2022, 12, 31))

    # --- a name with a split ----------------------------------------------
    _insert_security(conn, "SPLITCO", name="Splitco Corp", first_trade=date(2010, 6, 1),
                     known_from=start)
    split_index = len(session_dates) // 2
    split_ex = session_dates[split_index]
    # Raw prices must actually halve on the ex-date. Storing a smooth price path
    # alongside a split record would be data that could not have existed, and
    # any engine reading it would show the portfolio doubling overnight.
    splitco_closes = [
        round(price / 2.0, 4) if index >= split_index else price
        for index, price in enumerate(_walk(31, n, 600.0, 0.0006, 0.016))
    ]
    # On the ex-date the open is already post-split, so it comes from that
    # session's own close rather than the previous (pre-split) one.
    splitco_opens = [
        round(splitco_closes[i] * 0.999 if i == split_index
              else splitco_closes[max(i - 1, 0)] * 1.001, 4)
        for i in range(n)
    ]
    _insert_bars(conn, "SPLITCO", session_dates, splitco_closes, opens=splitco_opens)
    conn.execute(
        """INSERT OR REPLACE INTO corporate_actions
           (ticker, ex_date, action_type, ratio, cash_amount, announced_at, available_at, source)
           VALUES (?,?,?,?,?,?,?,?)""",
        ["SPLITCO", split_ex, "split", 2.0, None,
         datetime.combine(split_ex - timedelta(days=30), CLOSE_TIME),
         datetime.combine(split_ex - timedelta(days=30), CLOSE_TIME), "synthetic"],
    )

    # --- a selloff-and-recovery shape --------------------------------------
    _insert_security(conn, "FADECO", name="Fadeco PLC", sector="Consumer Discretionary",
                     sector_etf="XLY", first_trade=date(2012, 3, 1), known_from=start)
    _insert_bars(conn, "FADECO", session_dates, _shock_path(n))
    _insert_news_burst(conn, "FADECO", session_dates)

    # --- the required rejection case (§8.3) --------------------------------
    _seed_exro(conn, session_dates)

    # --- a name that delists mid-sample (survivorship, §8.3) ---------------
    gone_last = session_dates[int(n * 0.6)]
    _insert_security(conn, "GONECO", name="Goneco Inc", first_trade=date(2015, 1, 5),
                     known_from=start, last_trade=gone_last, delisted=gone_last,
                     delisting_reason="acquired", exchange="NYSE")
    gone_sessions = [s for s in session_dates if s <= gone_last]
    _insert_bars(conn, "GONECO", gone_sessions, _walk(41, len(gone_sessions), 55.0, 0.0002, 0.02))

    return {
        "sessions": n,
        "tickers": 6,
        "start": session_dates[0],
        "end": session_dates[-1],
    }


def _shock_path(n: int) -> list[float]:
    """Quiet, then a sharp multi-day selloff, then a slow grind back.

    Shaped so the §5.1 shock trigger and the §5.2 decay trigger have something
    to fire on once Phase 1 builds them.
    """
    rng = random.Random(77)
    prices, price = [], 210.0
    shock_start = int(n * 0.45)
    for i in range(n):
        if shock_start <= i < shock_start + 8:
            price *= 1.0 - abs(rng.gauss(0.022, 0.006))
        elif shock_start + 8 <= i < shock_start + 60:
            price *= 1.0 + abs(rng.gauss(0.0022, 0.004))
        else:
            price *= 1.0 + rng.gauss(0.0003, 0.011)
        prices.append(round(max(price, 0.01), 4))
    return prices


def _insert_news_burst(conn: duckdb.DuckDBPyConnection, ticker: str,
                       session_dates: list[date]) -> None:
    """A background hum of coverage, then a spike that decays — the volume
    pattern §5.1 and §5.2 are written against."""
    rng = random.Random(99)
    shock_index = int(len(session_dates) * 0.45)
    rows = []
    for index, session in enumerate(session_dates):
        distance = index - shock_index
        if 0 <= distance < 25:
            count = max(1, int(30 * (0.86 ** distance)))
        else:
            count = rng.randint(0, 2)
        for article in range(count):
            stamp = datetime.combine(session, time(9, min(59, 10 + article)))
            rows.append((
                f"{ticker}-{session:%Y%m%d}-{article}", ticker, stamp, "synthetic-wire",
                f"{ticker} in the news ({session})", None, None, stamp,
                datetime.now(), "synthetic",
            ))
    conn.executemany(
        """INSERT OR REPLACE INTO news_articles
           (article_id, ticker, published_at, source_name, headline, url, body_ref,
            available_at, ingested_at, source)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )


def _insert_10k(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    *,
    filing_date: date,
    period_end: date,
    going_concern: bool = False,
    delisting_notice: bool = False,
    default_or_covenant: bool = False,
    form_type: str = "10-K",
) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO filings
           (accession, cik, ticker, form_type, period_end, filing_date, accepted_at,
            available_at, primary_doc_url, going_concern_flag, delisting_notice_flag,
            default_or_covenant_flag, item_1a_ref, raw_ref, source)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            f"{ticker}-{form_type}-{filing_date:%Y%m%d}", None, ticker, form_type,
            period_end, filing_date, datetime.combine(filing_date, time(17, 30)),
            datetime.combine(filing_date, time(17, 30)), None,
            going_concern, delisting_notice, default_or_covenant, None, None, "synthetic",
        ],
    )


def _seed_exro(conn: duckdb.DuckDBPyConnection, session_dates: list[date]) -> None:
    """The case PRD §8.3 names by hand: the gate must reject it.

    Three independent reasons, so the test still means something if one input
    is missing: going-concern language in the last annual filing, negative
    trailing free cash flow with well under 18 months of runway, and a public
    delisting notice.
    """
    _insert_security(
        conn, "EXRO", name="Exro Technologies", sector="Industrials", sector_etf="XLI",
        first_trade=date(2019, 8, 15), known_from=session_dates[0],
        delisting_notice_date=date(2023, 6, 30), exchange="OTC",
    )
    _insert_bars(conn, "EXRO", session_dates, _walk(51, len(session_dates), 4.2, -0.0025, 0.035),
                 volume=400_000)

    quarters = _quarter_ends(2020, 16)
    # Burning roughly $22m a quarter against $60m of cash: about eight months
    # of runway, comfortably inside the 18-month bar in §4.2.
    _insert_quarters(conn, "EXRO", M.OPERATING_CASH_FLOW,
                     [(pe, fp, -20_000_000.0) for pe, fp in quarters])
    _insert_quarters(conn, "EXRO", M.CAPEX,
                     [(pe, fp, 2_000_000.0) for pe, fp in quarters])
    _insert_quarters(conn, "EXRO", M.CASH_AND_EQUIVALENTS,
                     [(pe, fp, 60_000_000.0) for pe, fp in quarters])
    _insert_quarters(conn, "EXRO", M.DEBT_DUE_WITHIN_24M,
                     [(pe, fp, 40_000_000.0) for pe, fp in quarters])
    _insert_quarters(conn, "EXRO", M.REVENUE,
                     [(pe, fp, 5_000_000.0) for pe, fp in quarters])

    _insert_10k(conn, "EXRO", filing_date=date(2023, 3, 30), period_end=date(2022, 12, 31),
                going_concern=True)
    _insert_10k(conn, "EXRO", filing_date=date(2023, 7, 5), period_end=date(2023, 6, 30),
                form_type="8-K", going_concern=True, delisting_notice=True)
