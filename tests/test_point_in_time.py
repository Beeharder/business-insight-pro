"""Tests for the point-in-time reader.

PRD §8.5: *"The point-in-time function needs its own dedicated tests; it is the
easiest thing to get subtly wrong and the most damaging."* This file is that.

Each test names a specific way historical data leaks the future. They are not
hypothetical — every one of them is a mistake that has been made in production
backtests, usually without anyone noticing until real money was involved.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from store import AsOf
from store.asof import FutureDataError


# -- the basic boundary ------------------------------------------------------


def test_asking_for_a_future_date_raises(pit_conn):
    """A lookahead bug should stop the run, not be silently clamped."""
    view = AsOf(pit_conn, date(2023, 5, 15))
    with pytest.raises(FutureDataError):
        view.close("PITCO", date(2023, 6, 15))


def test_bars_after_the_as_of_date_are_invisible(pit_conn):
    view = AsOf(pit_conn, date(2023, 5, 15))
    sessions = view.bars("PITCO")["session_date"].tolist()
    assert max(sessions) <= date(2023, 5, 15)
    assert date(2023, 6, 1) not in sessions


def test_todays_close_is_visible_because_the_cycle_runs_after_close(pit_conn):
    """§10.1 runs the cycle after the close, so the day's own bar is in scope."""
    view = AsOf(pit_conn, date(2023, 5, 15))
    assert view.close("PITCO", date(2023, 5, 15)) == 100.0


# -- filings: indexed by filing date, not period end -------------------------


def test_quarter_is_invisible_between_period_end_and_filing(pit_conn):
    """Q1 ends 31 March but is not filed until 10 May.

    A screen run in April that can see the Q1 number is reading a document that
    does not exist yet — the single most common fundamentals leak (§8.3).
    """
    april = AsOf(pit_conn, date(2023, 4, 15))
    assert april.fundamental("PITCO", "revenue") is None
    assert april.latest_filing("PITCO", "10-Q") is None


def test_quarter_becomes_visible_on_the_filing_date(pit_conn):
    may = AsOf(pit_conn, date(2023, 5, 11))
    assert may.fundamental("PITCO", "revenue") == 1_000.0
    assert may.latest_filing("PITCO", "10-Q")["accession"] == "acc-original"


# -- restatements ------------------------------------------------------------


def test_restatement_is_invisible_before_it_is_filed(pit_conn):
    """The August amendment must not touch a view dated in June."""
    june = AsOf(pit_conn, date(2023, 6, 15))
    assert june.fundamental("PITCO", "revenue") == 1_000.0


def test_as_reported_ignores_the_restatement_afterwards(pit_conn):
    """§8.3: fundamentals must use values as originally reported.

    Even in September, a screen that fires on reported figures should see the
    1,000 that was published in May — because that is the number the market
    reacted to at the time.
    """
    september = AsOf(pit_conn, date(2023, 9, 15))
    assert september.fundamental("PITCO", "revenue", vintage="as_reported") == 1_000.0


def test_latest_known_reflects_the_restatement_once_filed(pit_conn):
    """The other vintage, for asking what a careful reader believed."""
    september = AsOf(pit_conn, date(2023, 9, 15))
    assert september.fundamental("PITCO", "revenue", vintage="latest_known") == 800.0


def test_unknown_vintage_is_rejected(pit_conn):
    with pytest.raises(ValueError):
        AsOf(pit_conn, date(2023, 9, 15)).fundamental("PITCO", "revenue", vintage="whatever")


# -- trailing twelve months --------------------------------------------------


def test_ttm_refuses_to_return_a_partial_sum(pit_conn):
    """One quarter visible is not a TTM.

    Summing what happens to be there gives a number that is wrong rather than
    small, and §7.2 says refuse to trade on partial data.
    """
    assert AsOf(pit_conn, date(2023, 6, 1)).ttm("PITCO", "revenue") is None


def test_ttm_sums_four_quarters_once_they_are_all_filed(conn):
    from datetime import time

    quarters = [
        (date(2022, 6, 30), "Q2", 100.0, date(2022, 8, 5)),
        (date(2022, 9, 30), "Q3", 200.0, date(2022, 11, 5)),
        (date(2022, 12, 31), "Q4", 300.0, date(2023, 2, 5)),
        (date(2023, 3, 31), "Q1", 400.0, date(2023, 5, 10)),
    ]
    for period_end, label, value, filed in quarters:
        conn.execute(
            """INSERT INTO fundamentals
               (ticker, period_end, fiscal_period, metric, value, filing_date, available_at, source)
               VALUES (?,?,?,?,?,?,?,?)""",
            ["Q", period_end, label, "revenue", value, filed,
             datetime.combine(filed, time(16, 0)), "test"],
        )

    assert AsOf(conn, date(2023, 5, 9)).ttm("Q", "revenue") is None   # Q1 not filed yet
    assert AsOf(conn, date(2023, 5, 11)).ttm("Q", "revenue") == 1_000.0


# -- splits ------------------------------------------------------------------


def test_split_does_not_bend_prices_before_it_goes_ex(pit_conn):
    """The split is announced 1 May and goes ex 1 June.

    Seen from mid-May, the historical price is still 100. Back-adjusting it to
    50 as soon as the announcement lands would rewrite a chart the market had
    not yet redrawn — and would fire a 50% price-decline trigger on a company
    whose price never fell.
    """
    mid_may = AsOf(pit_conn, date(2023, 5, 15))
    assert mid_may.close("PITCO", date(2023, 5, 5), adjusted=True) == 100.0


def test_split_is_applied_to_history_once_it_is_ex(pit_conn):
    """From June, the May prices are correctly halved, so the series is
    comparable across the split instead of showing a phantom 50% crash."""
    june = AsOf(pit_conn, date(2023, 6, 15))
    assert june.close("PITCO", date(2023, 5, 5), adjusted=True) == 50.0
    assert june.close("PITCO", date(2023, 6, 15), adjusted=True) == 50.0


def test_unadjusted_prices_are_what_actually_printed(pit_conn):
    """Execution uses these: a real order in May filled at 100, not 50."""
    june = AsOf(pit_conn, date(2023, 6, 15))
    assert june.close("PITCO", date(2023, 5, 5), adjusted=False) == 100.0


def test_split_adjustment_preserves_traded_value(pit_conn):
    june = AsOf(pit_conn, date(2023, 6, 15))
    bars = june.bars("PITCO", start=date(2023, 5, 1), end=date(2023, 5, 31))
    assert all(abs(row.close * row.volume - 100_000_000) < 1e-6 for row in bars.itertuples())


# -- survivorship ------------------------------------------------------------


def test_a_company_that_delists_later_is_in_todays_universe(pit_conn):
    """§8.3, the survivorship trap.

    LATEGONE delists on 30 June 2023. A backtest of March 2023 that cannot buy
    it has quietly removed a loser from history, and every result computed from
    that universe is too good.
    """
    assert "LATEGONE" in AsOf(pit_conn, date(2023, 3, 1)).listed_tickers()


def test_a_delisted_company_leaves_the_universe_afterwards(pit_conn):
    assert "LATEGONE" not in AsOf(pit_conn, date(2023, 9, 1)).listed_tickers()
    assert AsOf(pit_conn, date(2023, 9, 1)).is_delisted("LATEGONE")


def test_delisting_is_not_known_in_advance(pit_conn):
    assert not AsOf(pit_conn, date(2023, 3, 1)).is_delisted("LATEGONE")


# -- news --------------------------------------------------------------------


def test_backfilled_article_does_not_create_a_retroactive_news_spike(pit_conn):
    """An archive that adds a story weeks later must not change what the §5.1
    volume trigger saw on the day."""
    on_the_day = AsOf(pit_conn, date(2023, 5, 5))
    counts = on_the_day.news_counts("PITCO", days=30)
    assert int(counts["articles"].sum()) == 1

    later = AsOf(pit_conn, date(2023, 5, 26))
    assert int(later.news_counts("PITCO", days=60)["articles"].sum()) == 2


def test_news_counts_are_bucketed_by_availability_not_publication(pit_conn):
    """The backfilled story counts on 25 May, when it appeared — not on 5 May,
    when it was written."""
    import pandas as pd

    later = AsOf(pit_conn, date(2023, 5, 26))
    counts = later.news_counts("PITCO", days=60)
    by_day = {
        pd.Timestamp(row.day).date(): int(row.articles) for row in counts.itertuples()
    }
    assert by_day[date(2023, 5, 5)] == 1
    assert by_day[date(2023, 5, 25)] == 1


# -- reference data corrections ----------------------------------------------


def test_a_later_correction_to_reference_data_is_invisible(conn):
    """Sector reclassifications and the like are dated too.

    Otherwise a §7.2 sector-exposure limit gets applied using a classification
    that did not exist at the time.
    """
    from datetime import time

    for sector, known_from in [("Technology", date(2020, 1, 1)), ("Industrials", date(2023, 1, 1))]:
        conn.execute(
            """INSERT INTO securities (ticker, name, sector, security_type,
                                       first_trade_date, available_at, source)
               VALUES (?,?,?,?,?,?,?)""",
            ["RECLASS", "Reclass Inc", sector, "common_stock", date(2015, 1, 2),
             datetime.combine(known_from, time(16, 0)), "test"],
        )

    assert AsOf(conn, date(2022, 6, 1)).security("RECLASS")["sector"] == "Technology"
    assert AsOf(conn, date(2023, 6, 1)).security("RECLASS")["sector"] == "Industrials"


def test_liquidity_returns_none_rather_than_a_short_window_average(pit_conn):
    """Ten sessions of history cannot answer a thirty-session question."""
    assert AsOf(pit_conn, date(2023, 5, 5)).avg_dollar_volume("PITCO", 30) is None


# -- the read cache ----------------------------------------------------------


def test_cache_never_changes_an_answer(pit_conn):
    """The cache holds unfiltered history and re-filters on every call.

    This test is the reason that design is safe to rely on. A cache shared
    across a whole backtest would be a perfect way to leak the future — one
    view's results reused by an earlier one — so a cached view and a cold view
    must agree on every date-sensitive question, including the ones that
    straddle the split, the filing and the delisting.
    """
    shared: dict = {}
    probes = [
        lambda v: v.close("PITCO", date(2023, 5, 5), adjusted=True),
        lambda v: v.close("PITCO", date(2023, 5, 5), adjusted=False),
        lambda v: v.fundamental("PITCO", "revenue"),
        lambda v: v.fundamental("PITCO", "revenue", vintage="latest_known"),
        lambda v: v.is_delisted("LATEGONE"),
        lambda v: tuple(v.listed_tickers()),
        lambda v: len(v.actions_on("PITCO", date(2023, 6, 1))),
        lambda v: (v.security("PITCO") or {}).get("sector"),
        lambda v: (v.bar("PITCO") or {}).get("session_date"),
    ]

    def outcome(probe, view):
        """Compare refusals as well as values — a cache that turned a
        FutureDataError into an answer would be the worst failure of all."""
        try:
            return ("value", probe(view))
        except FutureDataError:
            return ("refused", None)

    for as_of in [date(2023, 4, 15), date(2023, 5, 11), date(2023, 6, 1),
                  date(2023, 6, 15), date(2023, 9, 15)]:
        cold = AsOf(pit_conn, as_of)
        warm = AsOf(pit_conn, as_of, cache=shared)
        for probe in probes:
            assert outcome(probe, cold) == outcome(probe, warm), (
                f"cache changed an answer on {as_of}"
            )


def test_cache_is_shared_by_derived_views(pit_conn):
    shared: dict = {}
    view = AsOf(pit_conn, date(2023, 5, 15), cache=shared)
    view.close("PITCO")
    assert shared, "expected the first read to populate the cache"
    assert view.at(date(2023, 6, 15)).cache is shared
