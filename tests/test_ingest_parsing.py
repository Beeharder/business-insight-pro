"""Ingest parsing tests, run against recorded payload shapes.

The network calls themselves cannot be tested without credentials and a live
service, but the parsing can — and parsing is where the point-in-time
guarantees are either established or lost. A misread ``filed`` date does not
raise an exception; it produces a database that looks fine and backtests wrong.

The payloads below mirror the real response shapes from EDGAR and Alpaca,
trimmed to the fields the adapters actually read.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from ingest import filings, market, news
from store import AsOf

# -- EDGAR -------------------------------------------------------------------

SUBMISSIONS = {
    "cik": 320193,
    "filings": {
        "recent": {
            "accessionNumber": ["0000320193-23-000106", "0000320193-23-000064"],
            "filingDate": ["2023-11-03", "2023-08-04"],
            "reportDate": ["2023-09-30", "2023-07-01"],
            "acceptanceDateTime": ["2023-11-02T18:08:27.000Z", "2023-08-03T18:04:43.000Z"],
            "form": ["10-K", "10-Q"],
            "primaryDocument": ["aapl-20230930.htm", "aapl-20230701.htm"],
        }
    },
}

COMPANY_FACTS = {
    "cik": 320193,
    "facts": {
        "us-gaap": {
            "NetCashProvidedByUsedInOperatingActivities": {
                "units": {
                    "USD": [
                        {"end": "2023-09-30", "val": 110_543_000_000, "fy": 2023, "fp": "FY",
                         "form": "10-K", "filed": "2023-11-03", "accn": "0000320193-23-000106"},
                        # The same period, restated in an amendment a year later.
                        {"end": "2023-09-30", "val": 110_000_000_000, "fy": 2023, "fp": "FY",
                         "form": "10-K/A", "filed": "2024-06-01", "accn": "0000320193-24-000999"},
                    ]
                }
            }
        }
    },
}


def test_filings_are_indexed_by_filing_date_not_period_end():
    rows = filings.parse_submissions(SUBMISSIONS, "AAPL", 320193)
    annual = next(row for row in rows if row["form_type"] == "10-K")
    assert annual["period_end"] == date(2023, 9, 30)
    assert annual["filing_date"] == date(2023, 11, 3)


def test_availability_uses_acceptance_time_not_midnight():
    """A 10-K accepted at 18:08 was not public at that morning's open.

    Stamping it midnight would make it visible to a decision cycle that ran
    before it existed.
    """
    rows = filings.parse_submissions(SUBMISSIONS, "AAPL", 320193)
    annual = next(row for row in rows if row["form_type"] == "10-K")
    assert annual["available_at"] == datetime(2023, 11, 2, 18, 8, 27)


def test_primary_document_url_is_constructed():
    rows = filings.parse_submissions(SUBMISSIONS, "AAPL", 320193)
    url = rows[0]["primary_doc_url"]
    assert url.startswith("https://www.sec.gov/Archives/edgar/data/320193/")
    assert "-" not in url.rsplit("/", 2)[1]        # accession has dashes stripped


def test_company_facts_keep_both_vintages_of_a_restated_period():
    """§8.3 depends on this. If the amendment overwrote the original, there
    would be no way to ask what was reported at the time."""
    rows = filings.parse_company_facts(COMPANY_FACTS, "AAPL")
    same_period = [r for r in rows if r["period_end"] == date(2023, 9, 30)]
    assert len(same_period) == 2
    assert {r["filing_date"] for r in same_period} == {date(2023, 11, 3), date(2024, 6, 1)}
    assert sum(1 for r in same_period if r["is_restatement"]) == 1


def test_stored_facts_read_back_point_in_time(conn):
    """End to end: parse, store, and confirm the reader honours both dates."""
    filings.store_fundamentals(conn, filings.parse_company_facts(COMPANY_FACTS, "AAPL"))

    before = AsOf(conn, date(2023, 10, 1))
    assert before.fundamental("AAPL", "operating_cash_flow") is None

    after = AsOf(conn, date(2023, 12, 1))
    assert after.fundamental("AAPL", "operating_cash_flow") == 110_543_000_000

    later = AsOf(conn, date(2024, 7, 1))
    assert later.fundamental("AAPL", "operating_cash_flow") == 110_543_000_000
    assert later.fundamental("AAPL", "operating_cash_flow",
                             vintage="latest_known") == 110_000_000_000


# -- filing text flags -------------------------------------------------------


def test_going_concern_language_is_detected():
    text = """
        The accompanying financial statements have been prepared assuming the
        Company will continue as a going concern. As discussed in Note 2, the
        Company has incurred recurring losses, and these conditions raise
        substantial doubt about its ability to continue as a going concern.
    """
    assert filings.detect_flags(text)["going_concern_flag"] is True


def test_routine_going_concern_boilerplate_is_not_flagged():
    """Most filings mention the phrase while saying nothing is wrong.

    Flagging those would reject half the market, so the patterns require the
    language auditors use when they actually raise doubt.
    """
    text = """
        The financial statements have been prepared on a going concern basis.
        Management believes the Company has adequate liquidity to fund
        operations for at least the next twelve months.
    """
    assert filings.detect_flags(text)["going_concern_flag"] is False


def test_delisting_and_covenant_language_are_detected():
    delisting = "On 12 June the Company received a notice of delisting from the exchange."
    covenant = "The Company was not in compliance and an event of default occurred."
    assert filings.detect_flags(delisting)["delisting_notice_flag"] is True
    assert filings.detect_flags(covenant)["default_or_covenant_flag"] is True


def test_flag_detection_ignores_line_breaks_and_case():
    """Filing HTML wraps text at arbitrary points; a pattern that only matches
    single-line text would miss most real filings."""
    text = "these conditions\n   RAISE   SUBSTANTIAL\nDOUBT about the entity"
    assert filings.detect_flags(text)["going_concern_flag"] is True


# -- Alpaca bars -------------------------------------------------------------

ALPACA_BARS = [
    {"t": "2023-03-01T05:00:00Z", "o": 146.83, "h": 147.23, "l": 145.01,
     "c": 145.31, "v": 55_479_000, "n": 500_000, "vw": 146.0},
    {"t": "2023-03-02T05:00:00Z", "o": 144.38, "h": 146.71, "l": 143.90,
     "c": 145.91, "v": 52_238_000, "n": 480_000, "vw": 145.5},
]


def test_bars_are_stamped_available_at_session_close(conn):
    """Not at download time. Backfilling 2021 data today must not make it
    'available' in 2026 and invisible to every backtest before then."""
    market.store_bars(conn, "AAPL", ALPACA_BARS)
    row = conn.execute(
        "SELECT session_date, available_at FROM bars WHERE session_date = ?",
        [date(2023, 3, 1)],
    ).fetchone()
    assert row[0] == date(2023, 3, 1)
    assert row[1] == datetime(2023, 3, 1, 16, 0)


def test_stored_bars_read_back_correctly(conn):
    market.store_bars(conn, "AAPL", ALPACA_BARS)
    assert AsOf(conn, date(2023, 3, 2)).close("AAPL") == 145.91
    assert AsOf(conn, date(2023, 3, 1)).close("AAPL") == 145.31


# -- Alpaca corporate actions ------------------------------------------------


def test_forward_split_ratio_is_computed(conn):
    payload = {
        "forward_splits": [
            {"symbol": "NVDA", "ex_date": "2024-06-10", "new_rate": 10, "old_rate": 1,
             "declaration_date": "2024-05-22"},
        ]
    }
    market.store_corporate_actions(conn, payload)
    row = conn.execute(
        "SELECT ratio, action_type, available_at FROM corporate_actions"
    ).fetchone()
    assert row[0] == 10.0
    assert row[1] == "split"
    # Knowable from the announcement, weeks before it goes ex.
    assert row[2].date() == date(2024, 5, 22)


def test_reverse_split_ratio_is_below_one(conn):
    payload = {
        "reverse_splits": [
            {"symbol": "XYZ", "ex_date": "2023-04-10", "new_rate": 1, "old_rate": 10},
        ]
    }
    market.store_corporate_actions(conn, payload)
    assert conn.execute("SELECT ratio FROM corporate_actions").fetchone()[0] == 0.1


def test_cash_dividends_are_normalised(conn):
    payload = {
        "cash_dividends": [
            {"symbol": "KO", "ex_date": "2023-06-15", "rate": 0.46,
             "declaration_date": "2023-04-20"},
        ]
    }
    market.store_corporate_actions(conn, payload)
    row = conn.execute(
        "SELECT action_type, cash_amount, ratio FROM corporate_actions"
    ).fetchone()
    assert row[0] == "cash_dividend"
    assert row[1] == 0.46
    assert row[2] is None


# -- news --------------------------------------------------------------------

ALPACA_NEWS = [
    {"id": 1001, "created_at": "2023-05-05T13:30:00Z", "headline": "Company misses",
     "source": "benzinga", "url": "https://example.test/1", "symbols": ["AAA", "BBB"]},
    {"id": 1002, "created_at": "2023-05-06T14:00:00Z", "headline": "Analyst cuts",
     "source": "benzinga", "url": "https://example.test/2", "symbols": ["AAA"]},
]


def test_an_article_counts_once_per_tagged_ticker():
    """§5.1 asks about coverage of a company, not about articles in general."""
    rows = news.normalise_alpaca_articles(ALPACA_NEWS, ["AAA", "BBB"])
    assert len(rows) == 3
    assert sum(1 for row in rows if row["ticker"] == "AAA") == 2


def test_unrequested_tickers_are_dropped():
    rows = news.normalise_alpaca_articles(ALPACA_NEWS, ["AAA"])
    assert {row["ticker"] for row in rows} == {"AAA"}


def test_article_availability_is_publication_time():
    rows = news.normalise_alpaca_articles(ALPACA_NEWS, ["AAA"])
    assert rows[0]["available_at"] == rows[0]["published_at"]
    assert rows[0]["published_at"] == datetime(2023, 5, 5, 13, 30)


def test_article_ids_are_unique_per_ticker(conn):
    """One article tagged with two symbols becomes two rows, and neither may
    overwrite the other on insert."""
    news.store_articles(conn, news.normalise_alpaca_articles(ALPACA_NEWS, ["AAA", "BBB"]))
    assert conn.execute("SELECT count(*) FROM news_articles").fetchone()[0] == 3


def test_news_counts_read_back_by_day(conn):
    news.store_articles(conn, news.normalise_alpaca_articles(ALPACA_NEWS, ["AAA"]))
    counts = AsOf(conn, date(2023, 5, 7)).news_counts("AAA", days=10)
    assert int(counts["articles"].sum()) == 2


# -- transcripts -------------------------------------------------------------


def test_unconfigured_transcripts_raise_rather_than_return_nothing():
    """An empty list would tell Strategy B2 that tone never changed (§6.2),
    which is a much harder thing to notice than a configuration error."""
    from ingest.base import SourceUnavailable
    from ingest.transcripts import NoTranscriptSource

    with pytest.raises(SourceUnavailable, match="B2"):
        NoTranscriptSource().fetch("AAPL")
