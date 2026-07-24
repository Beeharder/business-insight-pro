"""Decision traces (PRD §8.4).

Every screen result, every model call, every order goes here, keyed so that any
past decision can be pulled back up by ticker and date:

    python cli.py logs --ticker NKE --date 2024-03-11

Two copies are written, on purpose:

* **DuckDB tables** — queryable, joinable, what the digest and the CLI read.
* **JSONL files under /logs** — one file per run, append-only, plain text.

The second copy exists because the first one is only useful if the process gets
far enough to commit a transaction. When something dies mid-run — which §8.5
requires us to survive — the JSONL file is what tells you where it died.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb

REPO_ROOT = Path(__file__).resolve().parent.parent


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "item"):          # numpy scalars
        return value.item()
    return str(value)


class DecisionLog:
    """Writes the trace for one run.

    A "run" is one execution of the daily cycle, or one backtest. Use it as a
    context manager so the run is closed out with a status even if it raises::

        with DecisionLog(conn, mode="paper") as log:
            log.screen("AAPL", stage="survivability", screen_name="going_concern", passed=True)
    """

    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        *,
        mode: str = "backtest",
        as_of: date | None = None,
        log_dir: str | Path = "logs",
        run_id: str | None = None,
        write_jsonl: bool = True,
    ) -> None:
        self.conn = conn
        self.run_id = run_id or f"{mode}-{datetime.now():%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
        self.mode = mode
        self.as_of = as_of or date.today()
        self.started_at = datetime.now()
        self.stages: dict[str, str] = {}
        self._jsonl = None

        if write_jsonl:
            directory = Path(log_dir)
            if not directory.is_absolute():
                directory = REPO_ROOT / directory
            directory.mkdir(parents=True, exist_ok=True)
            self._jsonl = (directory / f"{self.run_id}.jsonl").open("a", buffering=1)

        self.conn.execute(
            "INSERT INTO runs (run_id, mode, started_at, status, as_of_date) VALUES (?,?,?,?,?)",
            [self.run_id, self.mode, self.started_at, "running", self.as_of],
        )

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "DecisionLog":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        # Fail closed and fail loudly (PRD §9): a run that died is recorded as
        # failed with its error text, never left looking like it finished.
        self.finish(status="failed" if exc else "ok", error=repr(exc) if exc else None)
        return False

    def finish(self, *, status: str = "ok", error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at = ?, status = ?, error = ?, stages_json = ? WHERE run_id = ?",
            [datetime.now(), status, error, json.dumps(self.stages), self.run_id],
        )
        self._write_jsonl({"kind": "run_finished", "status": status, "error": error})
        if self._jsonl:
            self._jsonl.close()
            self._jsonl = None

    def stage(self, name: str, status: str = "ok") -> None:
        """Mark a pipeline stage done. The digest's health section lists these,
        so a stage that never reported is visible as a gap rather than silence."""
        self.stages[name] = status
        self._write_jsonl({"kind": "stage", "stage": name, "status": status})

    # -- writers -----------------------------------------------------------

    def screen(
        self,
        ticker: str | None,
        *,
        stage: str,
        screen_name: str | None = None,
        passed: bool | None = None,
        verdict: str | None = None,
        reason: str | None = None,
        strategy: str = "shared",
        as_of: date | None = None,
        **detail: Any,
    ) -> None:
        """Record one screen's verdict on one ticker."""
        row_date = as_of or self.as_of
        self.conn.execute(
            """
            INSERT INTO decision_log
                (log_id, run_id, ts, as_of_date, ticker, strategy, stage,
                 screen_name, passed, verdict, reason, detail_json)
            VALUES (nextval('decision_log_seq'),?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                self.run_id, datetime.now(), row_date, ticker, strategy, stage,
                screen_name, passed, verdict, reason,
                json.dumps(detail, default=_jsonable) if detail else None,
            ],
        )
        self._write_jsonl({
            "kind": "screen", "as_of": row_date, "ticker": ticker, "stage": stage,
            "screen": screen_name, "passed": passed, "verdict": verdict,
            "reason": reason, **detail,
        })

    def llm_call(
        self,
        *,
        ticker: str | None,
        model: str,
        prompt: str,
        response: str,
        schema_valid: bool,
        strategy: str | None = None,
        validation_error: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        latency_ms: int | None = None,
    ) -> str:
        """Store a model exchange verbatim.

        Prompt and response are kept in full, untruncated. When a classification
        looks wrong six weeks later, the question is almost always "what exactly
        did we ask it", and a summary cannot answer that.
        """
        call_id = uuid.uuid4().hex
        self.conn.execute(
            """
            INSERT INTO llm_calls
                (call_id, run_id, ts, ticker, strategy, model, prompt, response,
                 schema_valid, validation_error, input_tokens, output_tokens, latency_ms)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                call_id, self.run_id, datetime.now(), ticker, strategy, model,
                prompt, response, schema_valid, validation_error,
                input_tokens, output_tokens, latency_ms,
            ],
        )
        self._write_jsonl({
            "kind": "llm_call", "call_id": call_id, "ticker": ticker,
            "model": model, "schema_valid": schema_valid,
            "validation_error": validation_error,
        })
        return call_id

    def order(
        self,
        *,
        ticker: str,
        side: str,
        qty: float,
        strategy: str | None = None,
        order_type: str = "market_on_open",
        intended_at: date | None = None,
        status: str = "queued",
        fill_qty: float | None = None,
        fill_price: float | None = None,
        broker_order_id: str | None = None,
        reason: str | None = None,
        order_id: str | None = None,
    ) -> str:
        oid = order_id or uuid.uuid4().hex
        self.conn.execute(
            """
            INSERT INTO orders
                (order_id, run_id, ts, ticker, strategy, side, qty, order_type,
                 intended_at, status, fill_qty, fill_price, broker_order_id, reason)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                oid, self.run_id, datetime.now(), ticker, strategy, side, qty,
                order_type, intended_at, status, fill_qty, fill_price,
                broker_order_id, reason,
            ],
        )
        self._write_jsonl({
            "kind": "order", "order_id": oid, "ticker": ticker, "side": side,
            "qty": qty, "status": status, "fill_price": fill_price, "reason": reason,
        })
        return oid

    def update_order(self, order_id: str, **fields: Any) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{key} = ?" for key in fields)
        self.conn.execute(
            f"UPDATE orders SET {assignments} WHERE order_id = ?",
            [*fields.values(), order_id],
        )
        self._write_jsonl({"kind": "order_update", "order_id": order_id, **fields})

    # -- internals ---------------------------------------------------------

    def _write_jsonl(self, payload: dict[str, Any]) -> None:
        if not self._jsonl:
            return
        record = {"ts": datetime.now().isoformat(), "run_id": self.run_id, **payload}
        self._jsonl.write(json.dumps(record, default=_jsonable) + "\n")
        # Flush to the OS on every line. A crash should lose nothing that was
        # already decided — that is the whole point of the second copy.
        self._jsonl.flush()
        os.fsync(self._jsonl.fileno())


# -- reading the trace back out ---------------------------------------------


def decisions_for(
    conn: duckdb.DuckDBPyConnection,
    *,
    ticker: str | None = None,
    on: date | None = None,
    run_id: str | None = None,
):
    """Pull the trace for a ticker and/or a date. Backs ``cli.py logs``."""
    clauses, params = ["1=1"], []
    if ticker:
        clauses.append("ticker = ?")
        params.append(ticker)
    if on:
        clauses.append("as_of_date = ?")
        params.append(on)
    if run_id:
        clauses.append("run_id = ?")
        params.append(run_id)
    return conn.execute(
        f"""
        SELECT ts, as_of_date, ticker, strategy, stage, screen_name, passed,
               verdict, reason, detail_json
        FROM decision_log WHERE {' AND '.join(clauses)}
        ORDER BY as_of_date, ts, log_id
        """,
        params,
    ).df()
