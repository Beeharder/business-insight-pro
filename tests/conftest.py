"""Shared test fixtures.

Two worlds are available:

``demo_conn``
    The synthetic multi-ticker world from ``ingest.demo_data``. Used where a
    test needs realistic length and shape — backtests over three years.

``pit_conn``
    A tiny hand-built world, a handful of rows, every timestamp chosen
    deliberately. Used for the point-in-time tests, where the assertion is
    about one specific date boundary and a realistic dataset would only make
    the failure harder to read.
"""

from __future__ import annotations

from datetime import date, datetime, time

import pytest

from store import connect

CLOSE = time(16, 0)


@pytest.fixture
def conn():
    """An empty store, schema applied, in memory."""
    connection = connect(":memory:")
    yield connection
    connection.close()


@pytest.fixture(scope="session")
def demo_conn():
    """The synthetic world, seeded once for the whole suite.

    Session-scoped because seeding costs a few seconds and every test that uses
    it only reads. Tests that need to write use the ``conn`` fixture, which is
    a fresh empty database each time.
    """
    from ingest.demo_data import seed_demo

    connection = connect(":memory:")
    seed_demo(connection)
    yield connection
    connection.close()


@pytest.fixture
def pit_conn(conn):
    """A deliberately small world for testing the as-of boundaries.

    The dates are the point. Read them alongside the assertions:

    * ``PITCO`` reports Q1 2023 (period ending 31 March) in a 10-Q **filed on
      10 May**. Between those two dates the figure exists in the world but not
      in anyone's hands.
    * That Q1 figure is later **restated on 20 August**, so the same period has
      two values with different filing dates.
    * A 2-for-1 split goes ex on 1 June, announced 1 May.
    * ``LATEGONE`` trades until 30 June 2023 and then delists — so a view dated
      before that must still see it.
    """
    rows = [
        ("PITCO", date(2020, 1, 2), None, None, date(2019, 12, 1)),
        ("LATEGONE", date(2020, 1, 2), date(2023, 6, 30), date(2023, 6, 30), date(2019, 12, 1)),
    ]
    for ticker, first_trade, last_trade, delisted, known_from in rows:
        conn.execute(
            """INSERT INTO securities
               (ticker, name, exchange, sector, security_type, ipo_date, first_trade_date,
                last_trade_date, delisted_date, available_at, source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            [ticker, ticker, "NASDAQ", "Technology", "common_stock", first_trade,
             first_trade, last_trade, delisted, datetime.combine(known_from, CLOSE), "test"],
        )

    # Daily bars across the split, at a flat 100 before it and 50 after, so any
    # adjustment error shows up as a factor of two rather than a rounding blur.
    bar_rows = []
    for day in range(1, 29):
        session = date(2023, 5, day)
        if session.weekday() >= 5:
            continue
        bar_rows.append(("PITCO", session, 100.0, 100.0, 100.0, 100.0, 1_000_000))
    for day in range(1, 29):
        session = date(2023, 6, day)
        if session.weekday() >= 5:
            continue
        bar_rows.append(("PITCO", session, 50.0, 50.0, 50.0, 50.0, 2_000_000))
    for ticker, session, o, h, l, c, v in bar_rows:
        conn.execute(
            """INSERT INTO bars
               (ticker, session_date, open, high, low, close, volume, is_halted, available_at, source)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            [ticker, session, o, h, l, c, v, False, datetime.combine(session, CLOSE), "test"],
        )

    conn.execute(
        """INSERT INTO corporate_actions
           (ticker, ex_date, action_type, ratio, cash_amount, announced_at, available_at, source)
           VALUES (?,?,?,?,?,?,?,?)""",
        ["PITCO", date(2023, 6, 1), "split", 2.0, None,
         datetime.combine(date(2023, 5, 1), CLOSE),
         datetime.combine(date(2023, 5, 1), CLOSE), "test"],
    )

    # Q1 2023, filed 10 May, restated 20 August.
    conn.execute(
        """INSERT INTO fundamentals
           (ticker, period_end, fiscal_period, metric, value, unit, filing_date,
            accession, is_restatement, available_at, source)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        ["PITCO", date(2023, 3, 31), "Q1", "revenue", 1_000.0, "USD", date(2023, 5, 10),
         "acc-original", False, datetime.combine(date(2023, 5, 10), CLOSE), "test"],
    )
    conn.execute(
        """INSERT INTO fundamentals
           (ticker, period_end, fiscal_period, metric, value, unit, filing_date,
            accession, is_restatement, available_at, source)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        ["PITCO", date(2023, 3, 31), "Q1", "revenue", 800.0, "USD", date(2023, 8, 20),
         "acc-amended", True, datetime.combine(date(2023, 8, 20), CLOSE), "test"],
    )

    conn.execute(
        """INSERT INTO filings
           (accession, ticker, form_type, period_end, filing_date, accepted_at,
            available_at, going_concern_flag, delisting_notice_flag,
            default_or_covenant_flag, source)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        ["acc-original", "PITCO", "10-Q", date(2023, 3, 31), date(2023, 5, 10),
         datetime.combine(date(2023, 5, 10), time(17, 30)),
         datetime.combine(date(2023, 5, 10), time(17, 30)), False, False, False, "test"],
    )

    # One article published on 5 May, and one about 5 May that the archive only
    # made available on 25 May — the backfill case.
    conn.execute(
        """INSERT INTO news_articles
           (article_id, ticker, published_at, source_name, headline, available_at, source)
           VALUES (?,?,?,?,?,?,?)""",
        ["live-1", "PITCO", datetime(2023, 5, 5, 9, 30), "wire", "live story",
         datetime(2023, 5, 5, 9, 30), "test"],
    )
    conn.execute(
        """INSERT INTO news_articles
           (article_id, ticker, published_at, source_name, headline, available_at, source)
           VALUES (?,?,?,?,?,?,?)""",
        ["backfilled-1", "PITCO", datetime(2023, 5, 5, 10, 0), "wire", "backfilled story",
         datetime(2023, 5, 25, 12, 0), "test"],
    )

    return conn
