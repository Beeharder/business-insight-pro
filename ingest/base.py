"""Shared plumbing for talking to outside services.

Three things every adapter here gets for free:

**Timeouts.** Every request has one. A request without a timeout can hang
forever, and an unattended daily job that hangs looks exactly like a job that
is still working — which is the worst failure mode there is, because the
heartbeat digest in §9 never fires and nobody notices for a week.

**Retries with backoff.** Transient failures are normal. Four attempts with
growing pauses, and only on errors that are actually worth retrying.

**Fail closed.** When retries are exhausted the adapter raises. It does not
return an empty list. An empty list means "nothing happened today", and a
strategy that cannot tell that apart from "the news API was down" will read a
silent outage as a quiet news day (PRD §9, §7.2).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Iterable

import duckdb
import requests

DEFAULT_TIMEOUT = 30.0
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class IngestError(RuntimeError):
    """A source could not be read. Always fatal to the current stage."""


class SourceUnavailable(IngestError):
    """The source is not configured — no key, no provider chosen."""


@dataclass
class FetchStats:
    """What one ingest run did, for the watermark table and the digest."""

    source: str
    entity: str
    rows: int = 0
    covered_through: date | None = None
    error: str | None = None


class HttpClient:
    """A small requests wrapper with the three properties above.

    ``min_interval`` throttles outbound calls. SEC EDGAR asks for no more than
    ten requests a second and will block a client that ignores it, which is an
    unpleasant thing to discover halfway through a backfill.
    """

    def __init__(
        self,
        *,
        base_url: str = "",
        headers: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_attempts: int = 4,
        min_interval: float = 0.0,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.min_interval = min_interval
        self.session = session or requests.Session()
        if headers:
            self.session.headers.update(headers)
        self._last_call = 0.0

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict:
        response = self._get(path, params)
        try:
            return response.json()
        except ValueError as exc:
            raise IngestError(
                f"{path} returned something that is not JSON "
                f"(HTTP {response.status_code}, first 200 chars: {response.text[:200]!r})"
            ) from exc

    def get_text(self, path: str, params: dict[str, Any] | None = None) -> str:
        return self._get(path, params).text

    def _get(self, path: str, params: dict[str, Any] | None = None) -> requests.Response:
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        last_error: Exception | None = None

        for attempt in range(self.max_attempts):
            self._throttle()
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = exc
            else:
                if response.status_code < 400:
                    return response
                if response.status_code not in RETRYABLE_STATUS:
                    # 401, 403, 404 will not fix themselves. Stop immediately and
                    # say which, because the remedy differs: a key problem, a
                    # permissions problem, or a wrong URL.
                    raise IngestError(
                        f"GET {url} failed with HTTP {response.status_code}: "
                        f"{response.text[:200]!r}"
                    )
                last_error = IngestError(f"HTTP {response.status_code} from {url}")

            if attempt < self.max_attempts - 1:
                time.sleep(2 ** attempt)      # 1s, 2s, 4s

        raise IngestError(
            f"GET {url} failed after {self.max_attempts} attempts: {last_error}"
        )

    def _throttle(self) -> None:
        if not self.min_interval:
            return
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_call = time.monotonic()


def record_watermark(conn: duckdb.DuckDBPyConnection, stats: FetchStats) -> None:
    """Note how fresh a source is.

    The §7.2 "refuses to trade on stale, missing, or partial data" check and the
    digest's system-health section both read this table. A source that failed
    keeps its previous ``last_success_at``, so staleness accumulates visibly
    instead of resetting on every attempt.
    """
    now = datetime.now()
    existing = conn.execute(
        "SELECT last_success_at, covered_through, rows_written FROM ingest_watermarks "
        "WHERE source = ? AND entity = ?",
        [stats.source, stats.entity],
    ).fetchone()

    last_success = now if stats.error is None else (existing[0] if existing else None)
    covered = stats.covered_through or (existing[1] if existing else None)
    total_rows = (existing[2] or 0) + stats.rows if existing else stats.rows

    conn.execute(
        """
        INSERT OR REPLACE INTO ingest_watermarks
            (source, entity, last_success_at, last_attempt_at, covered_through,
             rows_written, last_error)
        VALUES (?,?,?,?,?,?,?)
        """,
        [stats.source, stats.entity, last_success, now, covered, total_rows, stats.error],
    )


def chunked(items: Iterable[Any], size: int) -> Iterable[list[Any]]:
    """Batch an iterable — most of these APIs take many symbols per call."""
    batch: list[Any] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
