"""Filings and fundamentals from SEC EDGAR.

EDGAR is free, authoritative, and — unusually for a free source — genuinely
point-in-time. Every fact in the ``companyfacts`` API carries a ``filed`` date
saying when it was submitted, and every restatement appears as a *separate*
fact for the same period with a later ``filed`` date. That is exactly the shape
PRD §8.3 asks for, and it is why this is worth parsing rather than paying for a
fundamentals vendor whose history has been quietly overwritten.

Two operational notes:

* EDGAR requires a descriptive ``User-Agent`` containing a real contact email.
  Requests without one get blocked, and the block is not always obvious.
* It asks for no more than ten requests a second. The client throttles below
  that on purpose; a backfill is not urgent and a block is expensive.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time
from typing import Iterable

import duckdb

from config import require_secret
from store.metrics import EDGAR_TAG_MAP

from .base import FetchStats, HttpClient, IngestError, record_watermark

SOURCE = "edgar"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"

# Phrases that indicate the §4.2 conditions. Deliberately narrow: a filing that
# merely uses the words "going concern" while describing accounting policy is
# not a company in trouble, so the patterns require the language auditors
# actually use when raising substantial doubt.
GOING_CONCERN_PATTERNS = [
    r"substantial\s+doubt\s+(?:exists\s+)?about\s+(?:the\s+)?(?:company|our|its)?\s*abilit\w+\s+to\s+continue\s+as\s+a\s+going\s+concern",
    r"substantial\s+doubt\s+about\s+its\s+ability\s+to\s+continue\s+as\s+a\s+going\s+concern",
    r"raise\s+substantial\s+doubt\s+about",
    r"conditions\s+.{0,60}raise\s+substantial\s+doubt",
]
DELISTING_PATTERNS = [
    r"notice\s+of\s+(?:potential\s+)?delisting",
    r"notification\s+of\s+(?:non-?compliance|deficiency)\s+.{0,80}listing",
    r"failure\s+to\s+satisfy\s+(?:a\s+)?continued\s+listing",
    r"minimum\s+bid\s+price\s+requirement",
]
DEFAULT_PATTERNS = [
    r"event\s+of\s+default",
    r"covenant\s+(?:breach|violation)",
    r"(?:breached|violated|failed\s+to\s+comply\s+with)\s+.{0,40}covenant",
    r"waiver\s+of\s+.{0,30}covenant",
]

_ANNUAL_OR_QUARTERLY = {"10-K", "10-K/A", "10-Q", "10-Q/A"}


def build_client(timeout: float = 30.0) -> HttpClient:
    user_agent = require_secret(
        "EDGAR_USER_AGENT",
        "identify yourself to SEC EDGAR (it blocks anonymous clients)",
    )
    return HttpClient(
        headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
        timeout=timeout,
        min_interval=0.15,   # ~7 requests/second, under EDGAR's limit of 10
    )


# -- reference data ----------------------------------------------------------


def fetch_ticker_map(client: HttpClient) -> dict[str, int]:
    """Ticker to CIK, for every company that files with the SEC."""
    payload = client.get_json(TICKERS_URL)
    return {
        entry["ticker"].upper(): int(entry["cik_str"])
        for entry in payload.values()
        if entry.get("ticker")
    }


# -- filings -----------------------------------------------------------------


def fetch_submissions(client: HttpClient, cik: int) -> dict:
    return client.get_json(SUBMISSIONS_URL.format(cik=cik))


def parse_submissions(payload: dict, ticker: str, cik: int) -> list[dict]:
    """Flatten EDGAR's column-oriented filing index into rows.

    The ``recent`` block holds roughly the last thousand filings as parallel
    arrays. Older filings live in separate files referenced under ``files``;
    for a universe of large caps with a two-year lookback, ``recent`` is
    normally enough, and this returns what it has rather than pretending
    otherwise.
    """
    recent = (payload.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    rows = []

    for index, form in enumerate(forms):
        accession = recent["accessionNumber"][index]
        filing_date = _as_date(recent["filingDate"][index])
        accepted_raw = (recent.get("acceptanceDateTime") or [None] * len(forms))[index]
        accepted = _as_datetime(accepted_raw) or datetime.combine(filing_date, time(17, 30))
        period_end = _as_date((recent.get("reportDate") or [None] * len(forms))[index])
        document = (recent.get("primaryDocument") or [None] * len(forms))[index]

        rows.append({
            "accession": accession,
            "cik": str(cik),
            "ticker": ticker,
            "form_type": form,
            "period_end": period_end,
            "filing_date": filing_date,
            "accepted_at": accepted,
            # Knowable from acceptance, not from the filing date at midnight: a
            # 10-K accepted at 17:30 was not public at 09:30 that morning.
            "available_at": accepted,
            "primary_doc_url": ARCHIVE_URL.format(
                cik=cik, accession=accession.replace("-", ""), document=document
            ) if document else None,
        })
    return rows


def detect_flags(text: str) -> dict[str, bool]:
    """Scan filing text for the §4.2 conditions.

    Regex rather than a model, on purpose. This runs before any LLM call, has
    to be deterministic and reproducible years later, and its failure mode
    should be a false positive — flagging a healthy company and rejecting it —
    rather than a false negative that lets a distressed one through.
    """
    lowered = re.sub(r"\s+", " ", text.lower())
    return {
        "going_concern_flag": _matches_any(lowered, GOING_CONCERN_PATTERNS),
        "delisting_notice_flag": _matches_any(lowered, DELISTING_PATTERNS),
        "default_or_covenant_flag": _matches_any(lowered, DEFAULT_PATTERNS),
    }


def _matches_any(text: str, patterns: Iterable[str]) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


def store_filings(conn: duckdb.DuckDBPyConnection, rows: Iterable[dict]) -> int:
    prepared = [
        (
            row["accession"], row.get("cik"), row["ticker"], row["form_type"],
            row.get("period_end"), row["filing_date"], row.get("accepted_at"),
            row["available_at"], row.get("primary_doc_url"),
            bool(row.get("going_concern_flag")), bool(row.get("delisting_notice_flag")),
            bool(row.get("default_or_covenant_flag")),
            row.get("item_1a_ref"), row.get("raw_ref"), SOURCE,
        )
        for row in rows
    ]
    if not prepared:
        return 0
    conn.executemany(
        """INSERT OR REPLACE INTO filings
           (accession, cik, ticker, form_type, period_end, filing_date, accepted_at,
            available_at, primary_doc_url, going_concern_flag, delisting_notice_flag,
            default_or_covenant_flag, item_1a_ref, raw_ref, source)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        prepared,
    )
    return len(prepared)


# -- fundamentals ------------------------------------------------------------


def fetch_company_facts(client: HttpClient, cik: int) -> dict:
    return client.get_json(COMPANYFACTS_URL.format(cik=cik))


def parse_company_facts(payload: dict, ticker: str) -> list[dict]:
    """Turn XBRL facts into ``fundamentals`` rows.

    The ``filed`` field on each fact is the whole reason this source is usable:
    it is the date the number became public. A restated figure arrives as
    another fact for the same ``end`` period with a later ``filed`` date, so
    both vintages survive and ``AsOf.fundamental`` can return either.
    """
    facts = (payload.get("facts") or {}).get("us-gaap") or {}
    cik = payload.get("cik")
    rows = []

    for tag, metric in EDGAR_TAG_MAP.items():
        tag_data = facts.get(tag)
        if not tag_data:
            continue
        for unit, entries in (tag_data.get("units") or {}).items():
            for entry in entries:
                period_end = _as_date(entry.get("end"))
                filed = _as_date(entry.get("filed"))
                if period_end is None or filed is None:
                    continue
                rows.append({
                    "cik": str(cik) if cik else None,
                    "ticker": ticker,
                    "period_end": period_end,
                    "fiscal_period": entry.get("fp"),
                    "metric": metric,
                    "value": float(entry["val"]),
                    "unit": unit,
                    "filing_date": filed,
                    "accession": entry.get("accn"),
                    "is_restatement": str(entry.get("form", "")).endswith("/A"),
                    "available_at": datetime.combine(filed, time(17, 30)),
                })
    return rows


def store_fundamentals(conn: duckdb.DuckDBPyConnection, rows: Iterable[dict]) -> int:
    prepared = [
        (
            row.get("cik"), row["ticker"], row["period_end"], row.get("fiscal_period"),
            row["metric"], row["value"], row.get("unit"), row["filing_date"],
            row.get("accession"), bool(row.get("is_restatement")), row["available_at"], SOURCE,
        )
        for row in rows
    ]
    if not prepared:
        return 0
    conn.executemany(
        """INSERT OR REPLACE INTO fundamentals
           (cik, ticker, period_end, fiscal_period, metric, value, unit,
            filing_date, accession, is_restatement, available_at, source)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        prepared,
    )
    return len(prepared)


# -- orchestration -----------------------------------------------------------


def ingest_company(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    cik: int,
    *,
    client: HttpClient | None = None,
    scan_text_for_flags: bool = True,
    max_documents_scanned: int = 8,
) -> FetchStats:
    """Pull one company's filings and fundamentals into the store.

    Text scanning is limited to the most recent annual and quarterly reports.
    Downloading every document a large company has ever filed is slow and
    almost entirely wasted — the §4.2 gate only reads the latest ones.
    """
    client = client or build_client()
    stats = FetchStats(source=SOURCE, entity=f"filings:{ticker}")

    try:
        filing_rows = parse_submissions(fetch_submissions(client, cik), ticker, cik)

        if scan_text_for_flags:
            scannable = [r for r in filing_rows
                         if r["form_type"] in _ANNUAL_OR_QUARTERLY and r["primary_doc_url"]]
            for row in sorted(scannable, key=lambda r: r["filing_date"], reverse=True)[:max_documents_scanned]:
                try:
                    row.update(detect_flags(client.get_text(row["primary_doc_url"])))
                except IngestError as exc:
                    # One unreadable document must not sink the whole company.
                    # The flags default to False, which the §4.2 gate treats as
                    # "nothing found" — so record why, and let the missing-data
                    # branches of the gate do their job.
                    row["raw_ref"] = f"scan_failed: {exc}"

        stats.rows += store_filings(conn, filing_rows)
        stats.rows += store_fundamentals(
            conn, parse_company_facts(fetch_company_facts(client, cik), ticker)
        )
        if filing_rows:
            stats.covered_through = max(r["filing_date"] for r in filing_rows)
    except IngestError as exc:
        stats.error = str(exc)
        record_watermark(conn, stats)
        raise

    record_watermark(conn, stats)
    return stats


def _as_date(value) -> date | None:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def _as_datetime(value) -> datetime | None:
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        return None
