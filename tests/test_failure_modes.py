"""The boring failures (PRD §8.5, §9).

*"Simulate: market holidays, halted tickers, partial fills, API timeouts,
missing data, and a mid-run crash."*

PRD §13 is blunt about why: *"Unattended daily systems fail in boring ways."*
None of these is intellectually interesting, and every one of them will happen.
The standard each test holds the system to is §9's: fail closed, place no
orders, leave a trace that says what went wrong.
"""

from __future__ import annotations

from datetime import date, datetime, time

import pytest
import requests

from backtest import BacktestEngine, ExecutionSettings, OrderIntent, sessions
from backtest.trading_calendar import is_session, next_session
from ingest.base import HttpClient, IngestError, record_watermark, FetchStats
from store import AsOf, DecisionLog, connect

CASH = 100_000.0
CLOSE = time(16, 0)


# -- market holidays ---------------------------------------------------------


def test_holidays_are_not_trading_sessions():
    assert not is_session(date(2023, 12, 25))      # Christmas
    assert not is_session(date(2023, 11, 23))      # Thanksgiving
    assert not is_session(date(2024, 3, 29))       # Good Friday


def test_unscheduled_closures_are_handled():
    """The market shut for two days for Hurricane Sandy. A hardcoded
    weekday-minus-holidays calendar gets this wrong; a real one does not."""
    assert not is_session(date(2012, 10, 29))
    assert not is_session(date(2012, 10, 30))


def test_next_session_skips_weekends_and_holidays():
    assert next_session(date(2023, 12, 22)) == date(2023, 12, 26)   # Christmas Monday
    assert next_session(date(2023, 7, 3)) == date(2023, 7, 5)       # 4 July


def test_backtest_only_steps_on_trading_days(demo_conn):
    from backtest import BuyAndHold

    result = BacktestEngine(demo_conn, starting_cash=CASH).run(
        BuyAndHold("SPY"), date(2023, 11, 20), date(2023, 11, 30)
    )
    dates = result.equity_curve["session_date"].tolist()
    assert date(2023, 11, 23) not in dates          # Thanksgiving
    assert date(2023, 11, 25) not in dates          # Saturday
    assert dates == sessions(date(2023, 11, 20), date(2023, 11, 30))


# -- halted tickers ----------------------------------------------------------


@pytest.fixture
def halted_conn(conn):
    """A ticker that trades, halts for three sessions, then resumes."""
    conn.execute(
        """INSERT INTO securities (ticker, name, security_type, first_trade_date,
                                   available_at, source)
           VALUES (?,?,?,?,?,?)""",
        ["HALT", "Halt Corp", "common_stock", date(2020, 1, 2),
         datetime.combine(date(2020, 1, 2), CLOSE), "test"],
    )
    for session in sessions(date(2023, 3, 1), date(2023, 3, 31)):
        halted = date(2023, 3, 8) <= session <= date(2023, 3, 10)
        conn.execute(
            """INSERT INTO bars (ticker, session_date, open, high, low, close,
                                 volume, is_halted, available_at, source)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ["HALT", session, 50.0, 50.0, 50.0, 50.0, 1_000_000, halted,
             datetime.combine(session, CLOSE), "test"],
        )
    return conn


def test_halted_sessions_are_not_priced(halted_conn):
    """A halted session has no tradable price, so the reader returns nothing
    rather than a stale one dressed up as today's."""
    view = AsOf(halted_conn, date(2023, 3, 9))
    assert view.bar("HALT", date(2023, 3, 9)) is None


def test_latest_bar_skips_back_over_a_halt(halted_conn):
    view = AsOf(halted_conn, date(2023, 3, 9))
    assert view.bar("HALT")["session_date"] == date(2023, 3, 7)


class OrderOn:
    name = "order_on"

    def __init__(self, ticker, on, **kwargs):
        self.ticker, self.on, self.kwargs = ticker, on, kwargs

    def on_session(self, view, portfolio):
        if view.as_of != self.on:
            return []
        return [OrderIntent(ticker=self.ticker, side="buy", strategy=self.name, **self.kwargs)]


def test_order_into_a_halt_waits_then_fills_on_resumption(halted_conn):
    """Queued on the 7th, would fill on the 8th — but the 8th is halted. It
    retries and fills when trading resumes, rather than vanishing."""
    fills = []
    BacktestEngine(
        halted_conn, starting_cash=CASH,
        execution=ExecutionSettings(order_ttl_sessions=5),
        on_event=lambda k, p: fills.append(p) if k == "fill" else None,
    ).run(OrderOn("HALT", date(2023, 3, 7), notional=1_000.0), date(2023, 3, 1), date(2023, 3, 31))

    assert len(fills) == 1
    assert fills[0]["session"] == date(2023, 3, 13)


def test_order_is_abandoned_when_the_halt_outlasts_its_life(halted_conn):
    """A market-on-open order does not sit in the book for a week. It is
    dropped, and the reason is recorded — never silently forgotten."""
    result = BacktestEngine(
        halted_conn, starting_cash=CASH,
        execution=ExecutionSettings(order_ttl_sessions=2),
    ).run(OrderOn("HALT", date(2023, 3, 7), notional=1_000.0), date(2023, 3, 1), date(2023, 3, 31))

    assert len(result.unfilled) == 1
    assert "no tradable price" in result.unfilled[0]["reason"]
    assert not result.portfolio.positions


# -- partial fills -----------------------------------------------------------


def test_order_is_capped_by_available_volume(demo_conn):
    """A 5% position in a name that trades thinly cannot be filled in full.

    Assuming otherwise is how a backtest reports returns that no real order
    could have captured.
    """
    fills = []
    BacktestEngine(
        demo_conn, starting_cash=10_000_000.0,
        execution=ExecutionSettings(max_participation_of_volume=0.0001),
        on_event=lambda k, p: fills.append(p) if k == "fill" else None,
    ).run(OrderOn("BIGCO", date(2022, 3, 15), notional=5_000_000.0),
          date(2022, 3, 1), date(2022, 4, 30))

    assert len(fills) == 1
    assert fills[0]["qty"] == pytest.approx(5_000_000 * 0.0001)


def test_a_partial_fill_leaves_the_rest_uninvested(demo_conn):
    result = BacktestEngine(
        demo_conn, starting_cash=10_000_000.0,
        execution=ExecutionSettings(max_participation_of_volume=0.0001),
    ).run(OrderOn("BIGCO", date(2022, 3, 15), notional=5_000_000.0),
          date(2022, 3, 1), date(2022, 4, 30))

    assert result.portfolio.cash > 9_000_000.0


# -- API timeouts and outages ------------------------------------------------


class FlakySession:
    """A requests session that fails a set number of times, then succeeds."""

    def __init__(self, failures, exception=None, status=None):
        self.remaining = failures
        self.exception = exception
        self.status = status
        self.calls = 0
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        assert timeout is not None, "every request must carry a timeout"
        if self.remaining > 0:
            self.remaining -= 1
            if self.exception:
                raise self.exception
            return _Response(self.status or 503, "upstream unavailable")
        return _Response(200, '{"ok": true}')


class _Response:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text

    def json(self):
        import json
        return json.loads(self.text)


def test_every_request_carries_a_timeout():
    """Asserted inside FlakySession.get. An unattended job that hangs never
    sends its heartbeat digest, and silence is the alert (§9)."""
    session = FlakySession(failures=0)
    HttpClient(base_url="https://example.test", session=session).get_json("/thing")
    assert session.calls == 1


def test_transient_failures_are_retried():
    session = FlakySession(failures=2)
    client = HttpClient(base_url="https://example.test", session=session, max_attempts=4)
    assert client.get_json("/thing") == {"ok": True}
    assert session.calls == 3


def test_connection_timeouts_are_retried():
    session = FlakySession(failures=1, exception=requests.Timeout("timed out"))
    client = HttpClient(base_url="https://example.test", session=session, max_attempts=3)
    assert client.get_json("/thing") == {"ok": True}


def test_exhausted_retries_raise_rather_than_return_nothing(demo_conn):
    """The most important assertion in this file.

    Returning an empty result here would tell Strategy A that a company had a
    quiet news day, when in fact the news API was down. §5.2's decay trigger
    fires on falling article counts — so a silent outage would look exactly
    like the entry signal it is waiting for.
    """
    session = FlakySession(failures=99)
    client = HttpClient(base_url="https://example.test", session=session, max_attempts=2)
    with pytest.raises(IngestError):
        client.get_json("/thing")


def test_authentication_errors_fail_immediately():
    """A 401 will not fix itself, so retrying it just wastes four minutes."""
    session = FlakySession(failures=99, status=401)
    client = HttpClient(base_url="https://example.test", session=session, max_attempts=4)
    with pytest.raises(IngestError, match="401"):
        client.get_json("/thing")
    assert session.calls == 1


def test_a_failed_ingest_does_not_refresh_the_freshness_stamp(conn):
    """Staleness has to accumulate across failures, or a source that has been
    broken for a week looks current because something touched it today."""
    record_watermark(conn, FetchStats(source="news", entity="daily", rows=10))
    first = conn.execute(
        "SELECT last_success_at FROM ingest_watermarks WHERE source='news'"
    ).fetchone()[0]

    record_watermark(conn, FetchStats(source="news", entity="daily", error="boom"))
    row = conn.execute(
        "SELECT last_success_at, last_error FROM ingest_watermarks WHERE source='news'"
    ).fetchone()

    assert row[0] == first
    assert row[1] == "boom"


# -- missing data ------------------------------------------------------------


def test_missing_prices_return_none_rather_than_a_guess(conn):
    assert AsOf(conn, date(2023, 5, 1)).close("NOSUCH") is None
    assert AsOf(conn, date(2023, 5, 1)).staleness_days("NOSUCH") is None


def test_valuing_a_position_with_no_price_raises(conn):
    """Skipping an unpriceable holding would silently value it at zero and make
    a data outage look like a total loss."""
    from backtest.portfolio import Portfolio, Position

    portfolio = Portfolio(cash=0.0)
    portfolio.positions["GHOST"] = Position("GHOST", 10, 5.0, date(2023, 1, 1))
    with pytest.raises(KeyError):
        portfolio.equity({})


def test_staleness_is_measurable(demo_conn):
    """The §7.2 stale-data refusal needs a number to act on."""
    view = AsOf(demo_conn, date(2024, 1, 15))
    assert view.staleness_days("SPY") > 5


def test_backtest_over_a_range_with_no_sessions_raises(demo_conn):
    from backtest import BuyAndHold

    with pytest.raises(ValueError):
        BacktestEngine(demo_conn).run(
            BuyAndHold("SPY"), date(2023, 12, 30), date(2023, 12, 31)
        )


# -- mid-run crash -----------------------------------------------------------


def test_a_crash_marks_the_run_failed_and_keeps_the_trace(tmp_path):
    """§9: on any unhandled error, place no orders and alert.

    A run that dies must not be left looking like a run that finished. The
    digest reads this status, and 'still running' three days later is the
    signal that something is wrong.
    """
    conn = connect(":memory:")

    with pytest.raises(RuntimeError):
        with DecisionLog(conn, mode="test", log_dir=tmp_path) as log:
            log.screen("AAPL", stage="universe", passed=True)
            log.stage("universe")
            raise RuntimeError("simulated crash")

    run = conn.execute("SELECT status, error FROM runs").fetchone()
    assert run[0] == "failed"
    assert "simulated crash" in run[1]

    decisions = conn.execute("SELECT count(*) FROM decision_log").fetchone()[0]
    assert decisions == 1


def test_the_jsonl_trace_survives_a_crash(tmp_path):
    """The database copy is only useful if the process got far enough to commit.
    The plain-text trace is flushed and fsynced line by line, so it is what
    tells you where a killed process actually stopped (§8.4)."""
    conn = connect(":memory:")

    try:
        with DecisionLog(conn, mode="test", log_dir=tmp_path) as log:
            log.screen("AAPL", stage="universe", passed=True, note="before crash")
            raise RuntimeError("simulated crash")
    except RuntimeError:
        pass

    written = list(tmp_path.glob("*.jsonl"))
    assert len(written) == 1
    contents = written[0].read_text()
    assert "before crash" in contents
    assert "run_finished" in contents


def test_a_crashed_run_places_no_orders(tmp_path):
    conn = connect(":memory:")
    try:
        with DecisionLog(conn, mode="test", log_dir=tmp_path) as log:
            log.screen("AAPL", stage="limits", passed=False, reason="kill switch")
            raise RuntimeError("crash before ordering")
    except RuntimeError:
        pass

    assert conn.execute("SELECT count(*) FROM orders").fetchone()[0] == 0


def test_reruns_are_independent(tmp_path):
    """Each run gets its own id and its own file, so a re-run after a crash
    does not overwrite the evidence from the failed one."""
    conn = connect(":memory:")
    first = DecisionLog(conn, mode="test", log_dir=tmp_path)
    first.finish()
    second = DecisionLog(conn, mode="test", log_dir=tmp_path)
    second.finish()

    assert first.run_id != second.run_id
    assert len(list(tmp_path.glob("*.jsonl"))) == 2
