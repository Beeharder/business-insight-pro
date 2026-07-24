"""Daily bars and corporate actions from Alpaca.

One setting matters more than the rest: ``adjustment=raw``.

Alpaca will happily return split- and dividend-adjusted prices, and they are
adjusted using *today's* factors. Feed those into a backtest of 2021 and every
price before a 2023 split is silently wrong in a way that looks completely
plausible. We take raw prints, store the corporate actions separately with the
dates they were announced and went ex, and let the point-in-time reader apply
only the ones that had already happened (PRD §8.3).
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Iterable, Sequence

import duckdb

from config import secret
from store.asof import DEFAULT_DECISION_TIME

from .base import FetchStats, HttpClient, IngestError, SourceUnavailable, chunked, record_watermark

SOURCE = "alpaca"
DEFAULT_DATA_URL = "https://data.alpaca.markets"


def build_client(timeout: float = 30.0) -> HttpClient:
    key = secret("ALPACA_API_KEY_ID")
    api_secret = secret("ALPACA_API_SECRET_KEY")
    if not key or not api_secret:
        raise SourceUnavailable(
            "Alpaca keys are not set. Put ALPACA_API_KEY_ID and "
            "ALPACA_API_SECRET_KEY in your .env file — see .env.example."
        )
    return HttpClient(
        base_url=secret("ALPACA_DATA_URL", DEFAULT_DATA_URL) or DEFAULT_DATA_URL,
        headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": api_secret},
        timeout=timeout,
    )


def fetch_bars(
    client: HttpClient,
    symbols: Sequence[str],
    start: date,
    end: date,
    *,
    feed: str = "iex",
    page_limit: int = 10_000,
) -> dict[str, list[dict]]:
    """Daily bars for a batch of symbols, following pagination to the end.

    ``feed`` is "iex" on the free tier and "sip" on a paid one. IEX covers a
    single exchange, so its volumes are a fraction of consolidated volume — the
    §4.1 liquidity threshold has to be calibrated against whichever feed you
    actually use, or it will reject the entire universe.
    """
    out: dict[str, list[dict]] = {symbol: [] for symbol in symbols}
    page_token: str | None = None

    while True:
        params = {
            "symbols": ",".join(symbols),
            "timeframe": "1Day",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "adjustment": "raw",     # see the module docstring; do not change
            "feed": feed,
            "limit": page_limit,
        }
        if page_token:
            params["page_token"] = page_token

        payload = client.get_json("/v2/stocks/bars", params)
        for symbol, bars in (payload.get("bars") or {}).items():
            out.setdefault(symbol, []).extend(bars or [])

        page_token = payload.get("next_page_token")
        if not page_token:
            return out


def store_bars(
    conn: duckdb.DuckDBPyConnection,
    symbol: str,
    bars: Iterable[dict],
    *,
    decision_time: time = DEFAULT_DECISION_TIME,
) -> int:
    """Write bars, stamping when each became knowable.

    ``available_at`` is the session's close, not the moment we happened to
    download it. Stamping it with the download time would mean a backfill run
    today made 2021 data 'available' in 2026 and invisible to every backtest.
    """
    rows = []
    for bar in bars:
        session = _parse_session(bar["t"])
        rows.append((
            symbol, session, bar.get("o"), bar.get("h"), bar.get("l"), bar.get("c"),
            bar.get("v"), bar.get("n"), bar.get("vw"), False,
            datetime.combine(session, decision_time), SOURCE,
        ))
    if not rows:
        return 0
    conn.executemany(
        """INSERT OR REPLACE INTO bars
           (ticker, session_date, open, high, low, close, volume, trade_count,
            vwap, is_halted, available_at, source)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    return len(rows)


def fetch_corporate_actions(
    client: HttpClient, symbols: Sequence[str], start: date, end: date
) -> dict[str, list[dict]]:
    params = {
        "symbols": ",".join(symbols),
        "types": "forward_split,reverse_split,cash_dividend",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "limit": 1000,
    }
    payload = client.get_json("/v1/corporate-actions", params)
    return payload.get("corporate_actions") or {}


def store_corporate_actions(
    conn: duckdb.DuckDBPyConnection, actions_by_type: dict[str, list[dict]]
) -> int:
    """Normalise Alpaca's per-type action payloads into one table.

    ``available_at`` is the announcement date where one is given, and the
    ex-date otherwise. A split is knowable from the day it is announced, weeks
    before it goes ex, and a screen that only learns about it on the ex-date
    will misread the price gap as a crash.
    """
    rows = []
    for action_type, actions in actions_by_type.items():
        for action in actions:
            symbol = action.get("symbol")
            ex_date = _parse_session(action.get("ex_date") or action.get("effective_date"))
            if not symbol or ex_date is None:
                continue

            if "split" in action_type:
                old = float(action.get("old_rate") or 1)
                new = float(action.get("new_rate") or 1)
                ratio = new / old if old else 1.0
                normalised_type, cash = "split", None
            else:
                ratio, normalised_type = None, "cash_dividend"
                cash = float(action.get("rate") or 0)

            announced = _parse_session(action.get("declaration_date")) or ex_date
            rows.append((
                symbol, ex_date, normalised_type, ratio, cash,
                datetime.combine(announced, DEFAULT_DECISION_TIME),
                datetime.combine(announced, DEFAULT_DECISION_TIME), SOURCE,
            ))

    if not rows:
        return 0
    conn.executemany(
        """INSERT OR REPLACE INTO corporate_actions
           (ticker, ex_date, action_type, ratio, cash_amount, announced_at, available_at, source)
           VALUES (?,?,?,?,?,?,?,?)""",
        rows,
    )
    return len(rows)


def ingest_bars(
    conn: duckdb.DuckDBPyConnection,
    symbols: Sequence[str],
    start: date,
    end: date,
    *,
    client: HttpClient | None = None,
    feed: str = "iex",
    batch_size: int = 100,
) -> FetchStats:
    client = client or build_client()
    stats = FetchStats(source=SOURCE, entity="bars", covered_through=end)
    try:
        for batch in chunked(symbols, batch_size):
            for symbol, bars in fetch_bars(client, batch, start, end, feed=feed).items():
                stats.rows += store_bars(conn, symbol, bars)
    except IngestError as exc:
        stats.error = str(exc)
        record_watermark(conn, stats)
        raise
    record_watermark(conn, stats)
    return stats


def _parse_session(value) -> date | None:
    """Alpaca mixes '2023-04-05' and RFC-3339 timestamps across endpoints."""
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
