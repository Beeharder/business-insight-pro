"""News ingestion.

PRD §8.1 flags this as the binding constraint on Strategy A: the shock trigger
(§5.1) needs article *counts* against a trailing 90-day baseline, and the decay
trigger (§5.2) needs daily counts through the whole watch window. Neither can be
backtested further back than the archive goes. That is why §11 puts source
viability in Phase 0, before anything is built on top of it.

Counting, not reading
---------------------
Both triggers need volume, not text. Text is only needed for the handful of
candidates that reach the LLM stage in Phase 2. So an archive with good
coverage and cheap counts beats one with full article bodies and a low rate
limit — worth keeping in mind when comparing prices.

One provider is implemented here (Alpaca, since the account already exists for
market data). ``NewsSource`` is the interface to implement for another; nothing
downstream knows which provider produced a row.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Iterable, Protocol, Sequence

import duckdb

from config import secret

from .base import FetchStats, HttpClient, IngestError, SourceUnavailable, record_watermark


class NewsSource(Protocol):
    """What the rest of the system needs from a news provider."""

    name: str

    def fetch(self, symbols: Sequence[str], start: date, end: date) -> list[dict]:
        """Return article dicts with at least: id, ticker, published_at, headline."""

    def earliest_available(self, symbol: str) -> date | None:
        """Oldest article the archive will return — the backtest's hard floor."""


class AlpacaNews:
    """Alpaca's news endpoint.

    Included with the same account used for market data, which makes it the
    cheapest thing to evaluate first. Its archive depth and per-ticker coverage
    still have to be measured before Strategy A is backtested — run
    ``python cli.py check-sources`` and read docs/SOURCE_VIABILITY.md.
    """

    name = "alpaca_news"
    DEFAULT_URL = "https://data.alpaca.markets"

    def __init__(self, client: HttpClient | None = None) -> None:
        self.client = client or self._build_client()

    def _build_client(self) -> HttpClient:
        key = secret("ALPACA_API_KEY_ID")
        api_secret = secret("ALPACA_API_SECRET_KEY")
        if not key or not api_secret:
            raise SourceUnavailable(
                "Alpaca keys are not set; news cannot be fetched. See .env.example."
            )
        return HttpClient(
            base_url=secret("ALPACA_DATA_URL", self.DEFAULT_URL) or self.DEFAULT_URL,
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": api_secret},
        )

    def fetch(self, symbols: Sequence[str], start: date, end: date) -> list[dict]:
        articles: list[dict] = []
        page_token: str | None = None

        while True:
            params = {
                "symbols": ",".join(symbols),
                "start": start.isoformat(),
                "end": end.isoformat(),
                "limit": 50,
                "sort": "asc",
            }
            if page_token:
                params["page_token"] = page_token

            payload = self.client.get_json("/v1beta1/news", params)
            batch = payload.get("news") or []
            articles.extend(normalise_alpaca_articles(batch, symbols))

            page_token = payload.get("next_page_token")
            if not page_token or not batch:
                return articles

    def earliest_available(self, symbol: str) -> date | None:
        """Walk backwards a year at a time until the archive stops returning
        anything. Cheap, and it answers the only question that matters in
        Phase 0: how far back can Strategy A actually be tested."""
        probe_end = date.today()
        earliest: date | None = None
        for _ in range(20):                      # up to twenty years back
            probe_start = probe_end - timedelta(days=365)
            try:
                found = self.fetch([symbol], probe_start, probe_end)
            except IngestError:
                break
            if not found:
                break
            earliest = min(a["published_at"].date() for a in found)
            probe_end = probe_start
        return earliest


def normalise_alpaca_articles(batch: Iterable[dict], requested: Sequence[str]) -> list[dict]:
    """One row per (article, ticker).

    An article tagged with five symbols counts once for each of them, because
    the §5.1 trigger asks about coverage of *a company*, not about articles in
    the abstract.
    """
    wanted = {symbol.upper() for symbol in requested}
    rows = []
    for article in batch:
        created = _parse_ts(article.get("created_at"))
        if created is None:
            continue
        for symbol in article.get("symbols") or []:
            if wanted and symbol.upper() not in wanted:
                continue
            rows.append({
                "article_id": f"{article.get('id')}:{symbol.upper()}",
                "ticker": symbol.upper(),
                "published_at": created,
                "source_name": article.get("source"),
                "headline": article.get("headline"),
                "url": article.get("url"),
                "body_ref": None,
                # An archive that backfills a story later would let a news
                # spike appear on a day the market saw nothing, so availability
                # is pinned to publication, never to when we downloaded it.
                "available_at": created,
            })
    return rows


def store_articles(conn: duckdb.DuckDBPyConnection, rows: Iterable[dict]) -> int:
    prepared = [
        (
            row["article_id"], row["ticker"], row["published_at"], row.get("source_name"),
            row.get("headline"), row.get("url"), row.get("body_ref"),
            row["available_at"], datetime.now(), row.get("source", "news"),
        )
        for row in rows
    ]
    if not prepared:
        return 0
    conn.executemany(
        """INSERT OR REPLACE INTO news_articles
           (article_id, ticker, published_at, source_name, headline, url, body_ref,
            available_at, ingested_at, source)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        prepared,
    )
    return len(prepared)


def ingest_news(
    conn: duckdb.DuckDBPyConnection,
    source: NewsSource,
    symbols: Sequence[str],
    start: date,
    end: date,
) -> FetchStats:
    stats = FetchStats(source=source.name, entity="news", covered_through=end)
    try:
        stats.rows = store_articles(conn, source.fetch(symbols, start, end))
    except IngestError as exc:
        stats.error = str(exc)
        record_watermark(conn, stats)
        raise
    record_watermark(conn, stats)
    return stats


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        return None
