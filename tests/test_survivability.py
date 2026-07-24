"""Survivability gate tests (PRD §4.2), including the required EXRO case.

PRD §8.3 names one test explicitly: *"EXRO is a required test case. The
survivability gate must reject it. Write this as an actual test."* That is
``test_exro_is_rejected`` below.

The rest check each condition on its own, and — just as importantly — check
that missing data is treated as a failure rather than a pass. A gate that
quietly waves through companies it has no data on is worse than no gate,
because it looks like it is working.
"""

from __future__ import annotations

from datetime import date, datetime, time

import pytest

from screens import survivability_gate
from store import AsOf
from store import metrics as M

AS_OF = date(2023, 8, 1)
CLOSE = time(16, 0)


def _quarters(conn, ticker, metric, value, *, count=4, end_year=2023):
    """Four filed quarters of a metric, ending before the as-of date."""
    periods = [
        (date(2022, 9, 30), "Q3", date(2022, 11, 5)),
        (date(2022, 12, 31), "Q4", date(2023, 2, 5)),
        (date(2023, 3, 31), "Q1", date(2023, 5, 8)),
        (date(2023, 6, 30), "Q2", date(2023, 7, 28)),
    ][:count]
    for period_end, label, filed in periods:
        conn.execute(
            """INSERT INTO fundamentals
               (ticker, period_end, fiscal_period, metric, value, filing_date,
                available_at, source)
               VALUES (?,?,?,?,?,?,?,?)""",
            [ticker, period_end, label, metric, value, filed,
             datetime.combine(filed, CLOSE), "test"],
        )


def _security(conn, ticker, **kwargs):
    conn.execute(
        """INSERT INTO securities
           (ticker, name, security_type, first_trade_date, ipo_date,
            delisting_notice_date, available_at, source)
           VALUES (?,?,?,?,?,?,?,?)""",
        [ticker, ticker, "common_stock", date(2015, 1, 2), date(2015, 1, 2),
         kwargs.get("delisting_notice_date"),
         datetime.combine(date(2015, 1, 2), CLOSE), "test"],
    )


def _filing(conn, ticker, **flags):
    conn.execute(
        """INSERT INTO filings
           (accession, ticker, form_type, period_end, filing_date, accepted_at,
            available_at, going_concern_flag, delisting_notice_flag,
            default_or_covenant_flag, source)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        [f"{ticker}-{flags.get('tag', 'a')}", ticker, flags.get("form_type", "10-K"),
         date(2022, 12, 31), flags.get("filing_date", date(2023, 3, 1)),
         datetime.combine(flags.get("filing_date", date(2023, 3, 1)), time(17, 30)),
         datetime.combine(flags.get("filing_date", date(2023, 3, 1)), time(17, 30)),
         flags.get("going_concern", False), flags.get("delisting_notice", False),
         flags.get("default_or_covenant", False), "test"],
    )


def _healthy(conn, ticker="HEALTHY"):
    _security(conn, ticker)
    _filing(conn, ticker)
    _quarters(conn, ticker, M.OPERATING_CASH_FLOW, 500_000_000.0)
    _quarters(conn, ticker, M.CAPEX, 100_000_000.0)
    _quarters(conn, ticker, M.CASH_AND_EQUIVALENTS, 5_000_000_000.0)
    _quarters(conn, ticker, M.DEBT_DUE_WITHIN_24M, 1_000_000_000.0)
    return ticker


# -- the required case -------------------------------------------------------


def test_exro_is_rejected(demo_conn):
    """PRD §8.3's named requirement.

    EXRO fails on three independent grounds, which is the point: the gate does
    not depend on any single check being present in the data.
    """
    result = survivability_gate(AsOf(demo_conn, AS_OF), "EXRO")

    assert result.passed is False
    failed = {check.name for check in result.failures}
    assert "going_concern" in failed
    assert "cash_flow_or_runway" in failed
    assert "no_delisting_notice" in failed


def test_exro_was_already_rejected_before_the_delisting_notice(demo_conn):
    """Rejected in April too, months before the delisting notice.

    Worth asserting separately: a gate that only catches a company once it is
    publicly dying is not protecting anything.
    """
    result = survivability_gate(AsOf(demo_conn, date(2023, 4, 15)), "EXRO")
    assert result.passed is False
    assert "going_concern" in {check.name for check in result.failures}


def test_a_healthy_large_cap_passes(demo_conn):
    """The gate has to let something through, or it is not a gate."""
    assert survivability_gate(AsOf(demo_conn, AS_OF), "BIGCO").passed is True


# -- individual conditions ---------------------------------------------------


def test_going_concern_language_rejects(conn):
    ticker = _healthy(conn)
    _filing(conn, ticker, tag="gc", going_concern=True, filing_date=date(2023, 4, 1))
    result = survivability_gate(AsOf(conn, AS_OF), ticker)
    assert result.passed is False
    assert "going_concern" in {c.name for c in result.failures}


def test_short_runway_rejects(conn):
    _security(conn, "BURN")
    _filing(conn, "BURN")
    _quarters(conn, "BURN", M.OPERATING_CASH_FLOW, -50_000_000.0)
    _quarters(conn, "BURN", M.CAPEX, 5_000_000.0)
    _quarters(conn, "BURN", M.CASH_AND_EQUIVALENTS, 100_000_000.0)  # ~5 months
    _quarters(conn, "BURN", M.DEBT_DUE_WITHIN_24M, 0.0)

    result = survivability_gate(AsOf(conn, AS_OF), "BURN")
    assert result.passed is False
    runway = next(c for c in result.checks if c.name == "cash_flow_or_runway")
    assert runway.detail["runway_months"] < 18


def test_long_runway_passes_despite_negative_cash_flow(conn):
    """§4.2 allows either positive free cash flow *or* 18 months of runway."""
    _security(conn, "FUNDED")
    _filing(conn, "FUNDED")
    _quarters(conn, "FUNDED", M.OPERATING_CASH_FLOW, -20_000_000.0)
    _quarters(conn, "FUNDED", M.CAPEX, 0.0)
    _quarters(conn, "FUNDED", M.CASH_AND_EQUIVALENTS, 2_000_000_000.0)
    _quarters(conn, "FUNDED", M.DEBT_DUE_WITHIN_24M, 0.0)

    assert survivability_gate(AsOf(conn, AS_OF), "FUNDED").passed is True


def test_uncovered_near_term_debt_rejects(conn):
    _security(conn, "LEVERED")
    _filing(conn, "LEVERED")
    _quarters(conn, "LEVERED", M.OPERATING_CASH_FLOW, 100_000_000.0)
    _quarters(conn, "LEVERED", M.CAPEX, 10_000_000.0)
    _quarters(conn, "LEVERED", M.CASH_AND_EQUIVALENTS, 200_000_000.0)
    _quarters(conn, "LEVERED", M.DEBT_DUE_WITHIN_24M, 5_000_000_000.0)

    result = survivability_gate(AsOf(conn, AS_OF), "LEVERED")
    assert result.passed is False
    assert "debt_coverage" in {c.name for c in result.failures}


def test_covenant_breach_rejects(conn):
    ticker = _healthy(conn)
    _filing(conn, ticker, tag="cov", default_or_covenant=True, filing_date=date(2023, 5, 1))
    result = survivability_gate(AsOf(conn, AS_OF), ticker)
    assert "not_in_default" in {c.name for c in result.failures}


def test_delisting_notice_on_the_security_record_rejects(conn):
    conn.execute(
        """INSERT INTO securities (ticker, name, security_type, first_trade_date,
                                   delisting_notice_date, available_at, source)
           VALUES (?,?,?,?,?,?,?)""",
        ["NOTICE", "Notice Inc", "common_stock", date(2015, 1, 2), date(2023, 6, 1),
         datetime.combine(date(2015, 1, 2), CLOSE), "test"],
    )
    _filing(conn, "NOTICE")
    _quarters(conn, "NOTICE", M.OPERATING_CASH_FLOW, 100_000_000.0)
    _quarters(conn, "NOTICE", M.CAPEX, 0.0)
    _quarters(conn, "NOTICE", M.CASH_AND_EQUIVALENTS, 1_000_000_000.0)

    result = survivability_gate(AsOf(conn, AS_OF), "NOTICE")
    assert "no_delisting_notice" in {c.name for c in result.failures}


def test_delisting_notice_is_not_visible_before_it_is_issued(conn):
    """Point-in-time applies to the gate too — a June notice is not knowable
    in March, and a March backtest must not benefit from knowing it."""
    conn.execute(
        """INSERT INTO securities (ticker, name, security_type, first_trade_date,
                                   delisting_notice_date, available_at, source)
           VALUES (?,?,?,?,?,?,?)""",
        ["NOTICE", "Notice Inc", "common_stock", date(2015, 1, 2), date(2023, 6, 1),
         datetime.combine(date(2015, 1, 2), CLOSE), "test"],
    )
    _filing(conn, "NOTICE")
    _quarters(conn, "NOTICE", M.OPERATING_CASH_FLOW, 100_000_000.0)
    _quarters(conn, "NOTICE", M.CAPEX, 0.0)
    _quarters(conn, "NOTICE", M.CASH_AND_EQUIVALENTS, 1_000_000_000.0)
    _quarters(conn, "NOTICE", M.DEBT_DUE_WITHIN_24M, 0.0)

    march = survivability_gate(AsOf(conn, date(2023, 3, 15)), "NOTICE")
    assert "no_delisting_notice" not in {c.name for c in march.failures}


# -- fail closed -------------------------------------------------------------


def test_unknown_ticker_is_rejected(conn):
    """No data at all is a rejection, never a pass."""
    result = survivability_gate(AsOf(conn, AS_OF), "NOSUCH")
    assert result.passed is False
    assert len(result.failures) >= 3


def test_missing_cash_flow_data_is_rejected(conn):
    _security(conn, "SPARSE")
    _filing(conn, "SPARSE")
    result = survivability_gate(AsOf(conn, AS_OF), "SPARSE")
    assert result.passed is False
    assert "cash_flow_or_runway" in {c.name for c in result.failures}


def test_three_filed_quarters_is_not_enough(conn):
    """A partial TTM is a wrong number, not a small one (§7.2)."""
    _security(conn, "PARTIAL")
    _filing(conn, "PARTIAL")
    _quarters(conn, "PARTIAL", M.OPERATING_CASH_FLOW, 100_000_000.0, count=3)
    _quarters(conn, "PARTIAL", M.CAPEX, 0.0, count=3)
    _quarters(conn, "PARTIAL", M.CASH_AND_EQUIVALENTS, 1_000_000_000.0, count=3)

    result = survivability_gate(AsOf(conn, AS_OF), "PARTIAL")
    assert result.passed is False


def test_every_check_runs_even_after_one_fails(demo_conn):
    """The decision log needs the whole picture, not just the first rejection
    (§12.1 reviews these by hand months later)."""
    result = survivability_gate(AsOf(demo_conn, AS_OF), "EXRO")
    assert len(result.checks) == 5
    assert result.failure_summary
