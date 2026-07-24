"""The point-in-time reader — the single most important file in this repo.

PRD §8.3: *every* data access must go through a function that returns only what
was knowable as of a given date. This is that function, in the form of a small
object you bind to a date once and then ask questions of.

    view = AsOf(conn, date(2023, 6, 15))
    view.close("AAPL")          # the 15 June close, or the last one before it
    view.close("AAPL", date(2024, 1, 2))   # raises — that is in the future

Why this matters, in plain terms
--------------------------------
A backtest is a simulation of a decision you would have made on some past day.
If the simulation can see even slightly more than you could have seen that day,
it will produce results that look excellent and cannot be repeated with real
money. The leaks are rarely dramatic; they are things like using a revenue
figure that was restated two years later, or a share price series that has been
adjusted for a split that had not been announced yet.

The defence is structural rather than careful: nothing in this codebase reads
the database directly. Everything reads it through an ``AsOf`` bound to the
simulated date, and ``AsOf`` refuses to return anything stamped later than that.

Timestamps
----------
Every timestamp in the store is **naive US/Eastern**. There are no UTC values
mixed in. Mixing the two is a reliable way to get an off-by-a-few-hours leak
that nobody notices for months.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Iterable, Sequence

import duckdb
import pandas as pd

# The moment in the trading day at which the system makes decisions. Bars for
# the session are stamped as available at this time, so a run "after close" on
# day D can see D's close but nothing published later that evening.
DEFAULT_DECISION_TIME = time(16, 0)

QUARTERS = ("Q1", "Q2", "Q3", "Q4")


def _split_factor(session: date, splits: list[tuple[date, float]]) -> float:
    """Product of every split ratio going ex strictly after ``session``."""
    factor = 1.0
    for ex_date, ratio in splits:
        if ex_date > session:
            factor *= float(ratio)
    return factor


class FutureDataError(RuntimeError):
    """Raised when caller asks for data dated after the as-of date.

    This is a programming error, not a data problem, so it is loud. Silently
    clamping the request to the as-of date would hide the lookahead bug rather
    than surface it.
    """


@dataclass(frozen=True)
class AsOf:
    """A read-only view of the store, frozen at one instant in simulated time."""

    conn: duckdb.DuckDBPyConnection
    as_of: date
    decision_time: time = DEFAULT_DECISION_TIME
    cache: dict | None = field(default=None, compare=False, repr=False)

    # -- plumbing ----------------------------------------------------------

    @property
    def cutoff(self) -> datetime:
        """The instant this view can see up to, inclusive."""
        return datetime.combine(self.as_of, self.decision_time)

    def _cached(self, key: tuple, load: "Callable[[], Any]") -> Any:
        """Memoise a whole-history table slice across sibling views.

        A backtest builds one ``AsOf`` per session and asks most of them about
        the same handful of tickers, so the same rows get fetched hundreds of
        times. The cache holds the **unfiltered** history for a ticker, and
        every date filter is still applied afterwards, on every call. That
        distinction is what keeps the point-in-time guarantee intact: nothing
        date-dependent is ever cached, so a cached view and a cold one cannot
        disagree. There is a test for exactly that.

        The cache assumes nobody is writing to the store mid-run, which is true
        of backtests and of the daily cycle, where ingestion finishes before
        analysis starts. Pass no cache (the default) to disable it.
        """
        if self.cache is None:
            return load()
        if key not in self.cache:
            self.cache[key] = load()
        return self.cache[key]

    def _check_not_future(self, when: date | datetime | None, label: str) -> None:
        if when is None:
            return
        as_dt = when if isinstance(when, datetime) else datetime.combine(when, time.max)
        if as_dt.date() > self.as_of:
            raise FutureDataError(
                f"Asked for {label} dated {when}, but this view is as of {self.as_of}. "
                "Something is trying to read the future."
            )

    def _query(self, sql: str, params: Sequence[Any] = ()) -> pd.DataFrame:
        return self.conn.execute(sql, list(params)).df()

    def _one(self, sql: str, params: Sequence[Any] = ()) -> tuple | None:
        return self.conn.execute(sql, list(params)).fetchone()

    def at(self, as_of: date) -> "AsOf":
        """A new view at a different date. Used by the backtest loop each session."""
        return AsOf(self.conn, as_of, self.decision_time, self.cache)

    # -- securities --------------------------------------------------------

    def security(self, ticker: str) -> dict | None:
        """Reference data for a ticker as it was understood on the as-of date.

        Later corrections to the same ticker are invisible: only the newest row
        stamped at or before the cutoff is returned.
        """
        rows = self._cached(("security", ticker), lambda: self._load_security(ticker))
        cutoff = self.cutoff
        for available_at, row in reversed(rows):
            if available_at <= cutoff:
                return row
        return None

    def _load_security(self, ticker: str) -> list[tuple[datetime, dict]]:
        """Every version of a ticker's reference row, oldest first, undated."""
        df = self._query(
            "SELECT * FROM securities WHERE ticker = ? ORDER BY available_at", (ticker,)
        )
        return [
            (pd.Timestamp(row["available_at"]).to_pydatetime(), row.to_dict())
            for _, row in df.iterrows()
        ]

    def listed_tickers(self, *, tradable_only: bool = True) -> list[str]:
        """Tickers that existed as of this date.

        With ``tradable_only`` (the default) this excludes names that had already
        delisted by the as-of date, but it still *includes* names that delist
        later — which is exactly the survivorship-bias trap PRD §8.3 warns about.
        A backtest of June 2015 must be able to buy a company that went to zero
        in 2017, or its results are fiction.
        """
        sql = """
            WITH latest AS (
                SELECT *, row_number() OVER (
                    PARTITION BY ticker ORDER BY available_at DESC
                ) AS rn
                FROM securities
                WHERE available_at <= ?
            )
            SELECT ticker, first_trade_date, last_trade_date, delisted_date
            FROM latest WHERE rn = 1
        """
        df = self._query(sql, (self.cutoff,))
        if df.empty:
            return []
        if tradable_only:
            started = df["first_trade_date"].isna() | (
                pd.to_datetime(df["first_trade_date"]).dt.date <= self.as_of
            )
            ended = pd.Series(False, index=df.index)
            for col in ("last_trade_date", "delisted_date"):
                col_dates = pd.to_datetime(df[col])
                ended |= col_dates.notna() & (col_dates.dt.date < self.as_of)
            df = df[started & ~ended]
        return sorted(df["ticker"].tolist())

    def is_delisted(self, ticker: str) -> bool:
        sec = self.security(ticker)
        if not sec:
            return False
        for key in ("delisted_date", "last_trade_date"):
            value = sec.get(key)
            if value is not None and not pd.isna(value):
                if pd.Timestamp(value).date() < self.as_of:
                    return True
        return False

    # -- prices ------------------------------------------------------------

    def bars(
        self,
        ticker: str,
        *,
        start: date | None = None,
        end: date | None = None,
        sessions: int | None = None,
        adjusted: bool = True,
        include_halted: bool = False,
    ) -> pd.DataFrame:
        """Daily bars up to the as-of date.

        ``adjusted=True`` back-adjusts for splits that had already gone ex by the
        as-of date — and only those. A split announced tomorrow cannot bend
        yesterday's chart. ``adjusted=False`` gives the prices that actually
        printed, which is what the execution simulator uses, since a real order
        fills at the real price.

        ``sessions=N`` returns the most recent N sessions, which is how the §5.1
        and §5.2 windows are expressed.
        """
        self._check_not_future(end, "bars end")
        end = min(end or self.as_of, self.as_of)

        history = self._cached(("bars", ticker), lambda: self._load_bars(ticker))
        if history.empty:
            return history

        visible = history[
            (history["session_date"] <= end)
            & (history["available_at"] <= pd.Timestamp(self.cutoff))
        ]
        if start is not None:
            visible = visible[visible["session_date"] >= start]
        if not include_halted:
            visible = visible[~visible["is_halted"].fillna(False).astype(bool)]
        if sessions is not None:
            visible = visible.tail(int(sessions))

        df = visible.drop(columns=["available_at"]).reset_index(drop=True)
        if df.empty:
            return df
        return self._apply_splits(ticker, df) if adjusted else df

    def _load_bars(self, ticker: str) -> pd.DataFrame:
        """Whole bar history for a ticker, unfiltered by date.

        Deliberately unfiltered: the date filtering happens in ``bars`` on every
        call, so this can be reused across every session of a backtest without
        any view ever seeing rows it should not.
        """
        df = self._query(
            """
            SELECT session_date, open, high, low, close, volume, trade_count,
                   vwap, is_halted, available_at
            FROM bars WHERE ticker = ? ORDER BY session_date
            """,
            (ticker,),
        )
        if not df.empty:
            df["session_date"] = pd.to_datetime(df["session_date"]).dt.date
            df["available_at"] = pd.to_datetime(df["available_at"])
        return df

    def _apply_splits(self, ticker: str, df: pd.DataFrame) -> pd.DataFrame:
        """Back-adjust prices for splits already ex as of this view's date.

        For a bar on day t, the factor is the product of every split ratio with
        an ex-date after t and on or before the as-of date. Prices divide by it,
        volume multiplies by it, so the traded dollar value is unchanged.
        """
        applicable = self._visible_splits(ticker)
        if not applicable:
            return df

        out = df.copy()
        factor_series = pd.Series(
            [
                _split_factor(session, applicable)
                for session in out["session_date"]
            ],
            index=out.index,
        )
        for col in ("open", "high", "low", "close", "vwap"):
            if col in out.columns:
                out[col] = out[col] / factor_series
        if "volume" in out.columns:
            out["volume"] = out["volume"] * factor_series
        return out

    def bar(self, ticker: str, on: date | None = None, *, adjusted: bool = True) -> dict | None:
        """A single session's bar — the one on ``on``, or the latest available.

        This is the most-called method in the whole system: a backtest hits it
        several times per session per position. It works from plain dicts rather
        than going through ``bars``, because building a one-row DataFrame
        several thousand times costs more than everything else put together.
        The date filtering is identical.
        """
        self._check_not_future(on, "bar")
        sessions_list, by_date = self._cached(
            ("bars_by_date", ticker), lambda: self._load_bars_by_date(ticker)
        )
        if not sessions_list:
            return None

        cutoff = self.cutoff
        limit = min(on or self.as_of, self.as_of)

        if on is not None:
            row = by_date.get(on)
            if row is None or row["available_at"] > cutoff or row.get("is_halted"):
                return None
        else:
            index = bisect_right(sessions_list, limit) - 1
            row = None
            while index >= 0:
                candidate = by_date[sessions_list[index]]
                if candidate["available_at"] <= cutoff and not candidate.get("is_halted"):
                    row = candidate
                    break
                index -= 1
            if row is None:
                return None

        out = {key: value for key, value in row.items() if key != "available_at"}
        if adjusted:
            factor = self._split_factor_for(ticker, out["session_date"])
            if factor != 1.0:
                for key in ("open", "high", "low", "close", "vwap"):
                    if out.get(key) is not None:
                        out[key] = out[key] / factor
                if out.get("volume") is not None:
                    out["volume"] = out["volume"] * factor
        return out

    def _load_bars_by_date(self, ticker: str) -> tuple[list[date], dict[date, dict]]:
        history = self._cached(("bars", ticker), lambda: self._load_bars(ticker))
        if history.empty:
            return [], {}
        by_date: dict[date, dict] = {}
        for record in history.to_dict("records"):
            record["available_at"] = pd.Timestamp(record["available_at"]).to_pydatetime()
            by_date[record["session_date"]] = record
        return sorted(by_date), by_date

    def _split_factor_for(self, ticker: str, session: date) -> float:
        """Adjustment factor for one session, from splits already ex today."""
        splits = self._visible_splits(ticker)
        return _split_factor(session, splits) if splits else 1.0

    def _visible_splits(self, ticker: str) -> list[tuple[date, float]]:
        cutoff = self.cutoff
        return [
            (ex_date, ratio)
            for ex_date, ratio, available_at in self._cached(
                ("splits", ticker), lambda: self._load_splits(ticker)
            )
            if ex_date <= self.as_of and available_at <= cutoff
        ]

    def _load_splits(self, ticker: str) -> list[tuple[date, float, datetime]]:
        df = self._query(
            "SELECT ex_date, ratio, available_at FROM corporate_actions "
            "WHERE ticker = ? AND action_type = 'split' ORDER BY ex_date",
            (ticker,),
        )
        return [
            (
                pd.Timestamp(row["ex_date"]).date(),
                float(row["ratio"]),
                pd.Timestamp(row["available_at"]).to_pydatetime(),
            )
            for _, row in df.iterrows()
            if row["ratio"]
        ]

    def actions_on(self, ticker: str, on: date | None = None) -> list[dict]:
        """Corporate actions going ex on one date, as plain dicts.

        The list form of ``corporate_actions``. The backtest calls this once per
        session per holding, and on the overwhelming majority of days the answer
        is an empty list — which should cost nothing to produce.
        """
        self._check_not_future(on, "corporate action")
        ex_date = on or self.as_of
        by_ex_date = self._cached(("actions_by_date", ticker),
                                  lambda: self._load_actions_by_date(ticker))
        cutoff = self.cutoff
        return [
            action for action in by_ex_date.get(ex_date, ())
            if action["available_at"] <= cutoff
        ]

    def _load_actions_by_date(self, ticker: str) -> dict[date, list[dict]]:
        df = self._cached(("actions", ticker), lambda: self._load_actions(ticker))
        grouped: dict[date, list[dict]] = {}
        if df.empty:
            return grouped
        for record in df.to_dict("records"):
            record["available_at"] = pd.Timestamp(record["available_at"]).to_pydatetime()
            grouped.setdefault(record["ex_date"], []).append(record)
        return grouped

    def close(self, ticker: str, on: date | None = None, *, adjusted: bool = True) -> float | None:
        bar = self.bar(ticker, on, adjusted=adjusted)
        return None if bar is None else float(bar["close"])

    def open_price(self, ticker: str, on: date | None = None, *, adjusted: bool = False) -> float | None:
        """Opening print. Defaults to unadjusted because fills happen at real prices."""
        bar = self.bar(ticker, on, adjusted=adjusted)
        return None if bar is None else float(bar["open"])

    def avg_dollar_volume(self, ticker: str, window_days: int = 30) -> float | None:
        """Average daily dollar volume over the trailing window (§4.1)."""
        df = self.bars(ticker, sessions=window_days)
        if df.empty or len(df) < window_days:
            return None  # fail closed: too little history to judge liquidity
        return float((df["close"] * df["volume"]).mean())

    def index_close(self, symbol: str, on: date | None = None) -> float | None:
        """SPY or VIX level, for the §7.3 regime filter."""
        self._check_not_future(on, "index level")
        df = self._query(
            """
            SELECT close FROM index_levels
            WHERE symbol = ? AND session_date <= ? AND available_at <= ?
            ORDER BY session_date DESC LIMIT 1
            """,
            (symbol, min(on or self.as_of, self.as_of), self.cutoff),
        )
        return None if df.empty else float(df.iloc[0]["close"])

    def index_series(self, symbol: str, sessions: int) -> pd.DataFrame:
        df = self._query(
            """
            SELECT session_date, close FROM index_levels
            WHERE symbol = ? AND session_date <= ? AND available_at <= ?
            ORDER BY session_date DESC LIMIT ?
            """,
            (symbol, self.as_of, self.cutoff, int(sessions)),
        )
        return df.sort_values("session_date").reset_index(drop=True)

    # -- corporate actions -------------------------------------------------

    def corporate_actions(
        self, ticker: str, *, on: date | None = None, action_type: str | None = None
    ) -> pd.DataFrame:
        """Actions going ex on a given date (default: the as-of date).

        The backtest calls this each session to adjust share counts for splits
        and credit cash dividends.
        """
        self._check_not_future(on, "corporate action")
        ex_date = on or self.as_of

        all_actions = self._cached(("actions", ticker), lambda: self._load_actions(ticker))
        if all_actions.empty:
            return all_actions

        visible = all_actions[
            (all_actions["ex_date"] == ex_date)
            & (all_actions["available_at"] <= pd.Timestamp(self.cutoff))
        ]
        if action_type:
            visible = visible[visible["action_type"] == action_type]
        return visible.sort_values("action_type").reset_index(drop=True)

    def _load_actions(self, ticker: str) -> pd.DataFrame:
        """Whole corporate-action history for a ticker, unfiltered by date."""
        df = self._query(
            "SELECT * FROM corporate_actions WHERE ticker = ? ORDER BY ex_date", (ticker,)
        )
        if not df.empty:
            df["ex_date"] = pd.to_datetime(df["ex_date"]).dt.date
            df["available_at"] = pd.to_datetime(df["available_at"])
        return df

    # -- filings and fundamentals -----------------------------------------

    def latest_filing(self, ticker: str, form_type: str | None = None) -> dict | None:
        """Most recent filing the market had actually seen by the as-of date.

        Note the ordering key is ``filing_date``, never ``period_end`` (§8.3).
        """
        clauses = ["ticker = ?", "available_at <= ?"]
        params: list[Any] = [ticker, self.cutoff]
        if form_type:
            clauses.append("form_type = ?")
            params.append(form_type)
        df = self._query(
            f"""
            SELECT * FROM filings WHERE {' AND '.join(clauses)}
            ORDER BY filing_date DESC, accepted_at DESC LIMIT 1
            """,
            params,
        )
        return None if df.empty else df.iloc[0].to_dict()

    def filings(
        self, ticker: str, form_type: str | None = None, limit: int = 10
    ) -> pd.DataFrame:
        clauses = ["ticker = ?", "available_at <= ?"]
        params: list[Any] = [ticker, self.cutoff]
        if form_type:
            clauses.append("form_type = ?")
            params.append(form_type)
        return self._query(
            f"""
            SELECT * FROM filings WHERE {' AND '.join(clauses)}
            ORDER BY filing_date DESC LIMIT {int(limit)}
            """,
            params,
        )

    def fundamental(
        self,
        ticker: str,
        metric: str,
        *,
        period_end: date | None = None,
        vintage: str = "as_reported",
    ) -> float | None:
        """One reported figure, at the vintage you ask for.

        Two vintages, and the difference matters (PRD §8.3):

        ``as_reported`` (default)
            The number as the company **first** published it. Restatements are
            ignored. This is the honest input to a rule that fires on the day
            the figure lands.

        ``latest_known``
            The newest number visible on the as-of date, which after an
            amendment means the restated figure. Correct for asking "what did a
            careful reader believe on this date", wrong for backtesting a screen,
            because the restatement was not there when the screen ran.

        Both are still bounded by the cutoff, so neither can see the future.
        """
        if vintage not in ("as_reported", "latest_known"):
            raise ValueError(f"vintage must be 'as_reported' or 'latest_known', got {vintage!r}")
        self._check_not_future(period_end, "fundamental period_end")

        order = "ASC" if vintage == "as_reported" else "DESC"
        clauses = ["ticker = ?", "metric = ?", "available_at <= ?"]
        params: list[Any] = [ticker, metric, self.cutoff]
        if period_end is not None:
            clauses.append("period_end = ?")
            params.append(period_end)

        df = self._query(
            f"""
            WITH visible AS (
                SELECT * FROM fundamentals WHERE {' AND '.join(clauses)}
            ), newest_period AS (
                SELECT max(period_end) AS pe FROM visible
            )
            SELECT value FROM visible
            WHERE period_end = (SELECT pe FROM newest_period)
            ORDER BY filing_date {order} LIMIT 1
            """,
            params,
        )
        return None if df.empty else float(df.iloc[0]["value"])

    def ttm(self, ticker: str, metric: str, *, vintage: str = "as_reported") -> float | None:
        """Trailing-twelve-month sum over the last four *knowable* quarters.

        Returns ``None`` rather than a partial sum if fewer than four quarters
        had been filed by the as-of date. A three-quarter "TTM" is not a smaller
        number, it is a wrong one, and §7.2 says refuse to trade on partial data.
        """
        order = "ASC" if vintage == "as_reported" else "DESC"
        df = self._query(
            f"""
            WITH visible AS (
                SELECT * FROM fundamentals
                WHERE ticker = ? AND metric = ? AND available_at <= ?
                  AND fiscal_period IN ('Q1','Q2','Q3','Q4')
            ), picked AS (
                SELECT period_end, value, row_number() OVER (
                    PARTITION BY period_end ORDER BY filing_date {order}
                ) AS rn
                FROM visible
            )
            SELECT period_end, value FROM picked WHERE rn = 1
            ORDER BY period_end DESC LIMIT 4
            """,
            (ticker, metric, self.cutoff),
        )
        if len(df) < 4:
            return None
        return float(df["value"].sum())

    # -- text sources ------------------------------------------------------

    def news_counts(self, ticker: str, *, days: int = 90, end: date | None = None) -> pd.DataFrame:
        """Article counts per calendar day — the input to the §5.1 volume test
        and the §5.2 decay test.

        Counted by ``available_at``, not publication date, so an archive that
        backfills a story weeks later cannot retroactively create a news spike
        on a day the market saw nothing.
        """
        self._check_not_future(end, "news window end")
        end_date = min(end or self.as_of, self.as_of)
        start_date = end_date - timedelta(days=days - 1)
        return self._query(
            """
            SELECT CAST(available_at AS DATE) AS day, count(*) AS articles
            FROM news_articles
            WHERE ticker = ? AND available_at <= ? AND CAST(available_at AS DATE) >= ?
            GROUP BY 1 ORDER BY 1
            """,
            (ticker, min(datetime.combine(end_date, self.decision_time), self.cutoff), start_date),
        )

    def news_articles(self, ticker: str, *, days: int = 7, limit: int = 100) -> pd.DataFrame:
        start_date = self.as_of - timedelta(days=days - 1)
        return self._query(
            """
            SELECT article_id, ticker, published_at, source_name, headline, url, body_ref
            FROM news_articles
            WHERE ticker = ? AND available_at <= ? AND CAST(available_at AS DATE) >= ?
            ORDER BY available_at DESC LIMIT ?
            """,
            (ticker, self.cutoff, start_date, int(limit)),
        )

    def transcripts(self, ticker: str, *, limit: int = 5) -> pd.DataFrame:
        """Most recent earnings calls, newest first — the input to Strategy B2."""
        return self._query(
            """
            SELECT * FROM transcripts
            WHERE ticker = ? AND available_at <= ?
            ORDER BY event_date DESC LIMIT ?
            """,
            (ticker, self.cutoff, int(limit)),
        )

    def next_earnings_date(self, ticker: str) -> date | None:
        """Next scheduled earnings date as announced by the as-of date (§7.2)."""
        df = self._query(
            """
            SELECT scheduled_date FROM earnings_calendar
            WHERE ticker = ? AND available_at <= ? AND scheduled_date >= ?
            ORDER BY scheduled_date ASC, available_at DESC LIMIT 1
            """,
            (ticker, self.cutoff, self.as_of),
        )
        return None if df.empty else pd.Timestamp(df.iloc[0]["scheduled_date"]).date()

    # -- freshness ---------------------------------------------------------

    def staleness_days(self, ticker: str = "SPY") -> int | None:
        """Trading days between the newest visible bar and the as-of date.

        The §7.2 stale-data refusal reads this. ``None`` means no data at all,
        which is worse than stale and must also block trading.
        """
        row = self._one(
            """
            SELECT max(session_date) FROM bars
            WHERE ticker = ? AND session_date <= ? AND available_at <= ?
            """,
            (ticker, self.as_of, self.cutoff),
        )
        if not row or row[0] is None:
            return None
        return (self.as_of - pd.Timestamp(row[0]).date()).days
