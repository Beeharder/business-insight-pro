"""Earnings call transcripts — the input to Strategy B2 (§6.2).

**Status: no provider chosen.** This is deliberate, and it is a Phase 0
decision the PRD leaves open (§8.1: *"Verify availability and cost in Phase 0;
if unavailable, B2 is descoped and B1/B3 proceed"*).

Unlike prices and filings, there is no free authoritative source for
transcripts. They are produced by vendors, priced per call or per month, and
their archives differ sharply in how far back they go and which companies they
cover. Writing an adapter against a provider that has not been chosen would
mean guessing at an API contract that cannot be tested, so what exists here is
the interface and the evaluation harness, not a fake implementation.

What B2 actually needs, and what to check a provider against
-----------------------------------------------------------
1. **Five consecutive calls per company** — the latest plus the prior four
   (§6.2). A provider with gaps is worse than no provider, because the
   comparison silently changes meaning when a quarter is missing.
2. **Speaker-attributed text**, separating prepared remarks from Q&A. §6.2
   compares them differently, and a flat transcript cannot support that.
3. **A publication timestamp per transcript.** Without it the point-in-time
   guarantee breaks: transcripts usually appear hours to days after the call,
   and treating them as available at the call time is lookahead.
4. **Archive depth matching the news archive**, so A and B are backtested over
   the same period and §12.2's strategy attribution compares like with like.
5. **Licence terms permitting storage**, since §8.4 requires keeping raw inputs.

Candidates worth pricing: Financial Modeling Prep, API Ninjas, and Seeking
Alpha via RapidAPI. Record what you find in docs/SOURCE_VIABILITY.md.

If none of these clears the bar, set ``strategy_b.b2_tone_shift.enabled: false``
in config.yaml and proceed with B1 and B3, exactly as §8.1 provides for. B2 is
the only signal in the platform that depends on this source.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Iterable, Protocol, Sequence

import duckdb

from .base import FetchStats, IngestError, SourceUnavailable, record_watermark


class TranscriptSource(Protocol):
    """Implement this once a provider is chosen; nothing else needs to change."""

    name: str

    def fetch(self, symbol: str, limit: int = 5) -> list[dict]:
        """Most recent calls, newest first.

        Each dict needs: ``transcript_id``, ``ticker``, ``event_date``,
        ``fiscal_period``, ``text``, and ``available_at`` — the moment the
        transcript was *published*, not when the call took place.
        """

    def earliest_available(self, symbol: str) -> date | None:
        ...


class NoTranscriptSource:
    """The configured state until a provider is selected.

    Fails loudly rather than returning nothing. An empty list here would let
    B2 quietly conclude that management's tone never changes, which is a much
    harder problem to notice than a missing-configuration error.
    """

    name = "unconfigured"

    def fetch(self, symbol: str, limit: int = 5) -> list[dict]:
        raise SourceUnavailable(
            "No transcript provider is configured, so Strategy B2 cannot run. "
            "Either set TRANSCRIPTS_PROVIDER and TRANSCRIPTS_API_KEY in .env and "
            "implement the matching adapter, or set "
            "strategy_b.b2_tone_shift.enabled: false in config/config.yaml to "
            "descope B2 (PRD §8.1)."
        )

    def earliest_available(self, symbol: str) -> date | None:
        return None


def store_transcripts(conn: duckdb.DuckDBPyConnection, rows: Iterable[dict]) -> int:
    prepared = [
        (
            row["transcript_id"], row["ticker"], row["event_date"], row.get("fiscal_period"),
            row.get("call_type", "earnings"), row.get("text_ref"),
            row["available_at"], row.get("source", "transcripts"),
        )
        for row in rows
    ]
    if not prepared:
        return 0
    conn.executemany(
        """INSERT OR REPLACE INTO transcripts
           (transcript_id, ticker, event_date, fiscal_period, call_type, text_ref,
            available_at, source)
           VALUES (?,?,?,?,?,?,?,?)""",
        prepared,
    )
    return len(prepared)


def ingest_transcripts(
    conn: duckdb.DuckDBPyConnection,
    source: TranscriptSource,
    symbols: Sequence[str],
    *,
    limit: int = 5,
) -> FetchStats:
    stats = FetchStats(source=source.name, entity="transcripts")
    try:
        for symbol in symbols:
            stats.rows += store_transcripts(conn, source.fetch(symbol, limit))
    except (IngestError, SourceUnavailable) as exc:
        stats.error = str(exc)
        record_watermark(conn, stats)
        raise
    record_watermark(conn, stats)
    return stats
