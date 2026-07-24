"""Decision log tests (PRD §8.4).

*"Retrievable by ticker and date so any past trade can be reconstructed. This is
not optional — without it there is no way to diagnose or improve the system."*

§12.1's process gate is a manual review of these records at the three-month
mark. If the log cannot answer "why did it buy this, and what did it see", the
gate cannot be assessed at all.
"""

from __future__ import annotations

from datetime import date

import pytest

from store import DecisionLog, connect
from store.decision_log import decisions_for


@pytest.fixture
def log_conn(tmp_path):
    conn = connect(":memory:")
    return conn, tmp_path


def test_a_decision_is_retrievable_by_ticker_and_date(log_conn):
    conn, tmp = log_conn
    with DecisionLog(conn, mode="paper", as_of=date(2024, 3, 11), log_dir=tmp) as log:
        log.screen("NKE", stage="shock", screen_name="price_decline", passed=True,
                   decline_pct=-0.14)
        log.screen("NKE", stage="survivability", screen_name="going_concern", passed=True)
        log.screen("KO", stage="shock", screen_name="price_decline", passed=False)

    nike = decisions_for(conn, ticker="NKE", on=date(2024, 3, 11))
    assert len(nike) == 2
    assert set(nike["stage"]) == {"shock", "survivability"}

    everything = decisions_for(conn, on=date(2024, 3, 11))
    assert len(everything) == 3


def test_screen_details_survive_as_structured_data(log_conn):
    """The numbers behind a verdict, not just the verdict.

    Reviewing a rejection months later means asking "how close was it" — which
    needs the measured value, not a boolean.
    """
    conn, tmp = log_conn
    with DecisionLog(conn, mode="paper", as_of=date(2024, 3, 11), log_dir=tmp) as log:
        log.screen("NKE", stage="shock", screen_name="price_decline", passed=True,
                   decline_pct=-0.14, threshold=-0.12, window_days=10)

    import json
    detail = json.loads(decisions_for(conn, ticker="NKE").iloc[0]["detail_json"])
    assert detail["decline_pct"] == -0.14
    assert detail["threshold"] == -0.12


def test_llm_prompt_and_response_are_stored_verbatim(log_conn):
    """§8.4 requires the raw prompt and raw response.

    When a classification looks wrong weeks later, the first question is what
    exactly the model was asked, and a summary cannot answer it.
    """
    conn, tmp = log_conn
    prompt = "Does this story change the company's future cash flows?\n\n" + "x" * 5000
    response = '{"anger_target": "person", "confidence": 0.82}'

    with DecisionLog(conn, mode="paper", log_dir=tmp) as log:
        log.llm_call(ticker="NKE", model="claude-haiku-4-5", prompt=prompt,
                     response=response, schema_valid=True, strategy="A",
                     input_tokens=1200, output_tokens=95, latency_ms=430)

    row = conn.execute("SELECT prompt, response, schema_valid FROM llm_calls").fetchone()
    assert row[0] == prompt          # untruncated
    assert row[1] == response
    assert row[2] is True


def test_a_schema_violation_is_recorded_not_discarded(log_conn):
    """§12.1 asks whether the model produced confident nonsense. That question
    is unanswerable if malformed responses were thrown away."""
    conn, tmp = log_conn
    with DecisionLog(conn, mode="paper", log_dir=tmp) as log:
        log.llm_call(ticker="NKE", model="claude-haiku-4-5", prompt="p",
                     response="not json at all", schema_valid=False,
                     validation_error="Expecting value: line 1 column 1")

    row = conn.execute("SELECT response, schema_valid, validation_error FROM llm_calls").fetchone()
    assert row[1] is False
    assert "Expecting value" in row[2]


def test_orders_are_tagged_with_their_strategy(log_conn):
    """§7.1: positions tagged by originating strategy so performance is
    attributable — the comparison §12.2 turns on."""
    conn, tmp = log_conn
    with DecisionLog(conn, mode="paper", as_of=date(2024, 3, 12), log_dir=tmp) as log:
        order_id = log.order(ticker="NKE", side="buy", qty=120, strategy="A",
                             intended_at=date(2024, 3, 13), reason="decay trigger fired")
        log.update_order(order_id, status="filled", fill_qty=120, fill_price=94.15)

    row = conn.execute(
        "SELECT strategy, status, fill_price, reason FROM orders"
    ).fetchone()
    assert row[0] == "A"
    assert row[1] == "filled"
    assert row[2] == 94.15
    assert row[3] == "decay trigger fired"


def test_stages_are_recorded_so_a_missing_one_is_visible(log_conn):
    """§10.2's health section reports which stages ran. A stage that never
    reported shows up as a gap rather than as silence."""
    conn, tmp = log_conn
    with DecisionLog(conn, mode="paper", log_dir=tmp) as log:
        log.stage("screen_universe")
        log.stage("survivability")
        log.stage("fetch_news", status="failed")

    import json
    stages = json.loads(conn.execute("SELECT stages_json FROM runs").fetchone()[0])
    assert stages["screen_universe"] == "ok"
    assert stages["fetch_news"] == "failed"
    assert "llm_classification" not in stages


def test_a_successful_run_is_marked_ok(log_conn):
    conn, tmp = log_conn
    with DecisionLog(conn, mode="paper", log_dir=tmp) as log:
        log.screen("NKE", stage="universe", passed=True)

    assert conn.execute("SELECT status FROM runs").fetchone()[0] == "ok"


def test_traces_from_different_runs_stay_separate(log_conn):
    conn, tmp = log_conn
    with DecisionLog(conn, mode="paper", as_of=date(2024, 3, 11), log_dir=tmp) as first:
        first.screen("NKE", stage="universe", passed=True)
        first_id = first.run_id
    with DecisionLog(conn, mode="paper", as_of=date(2024, 3, 12), log_dir=tmp) as second:
        second.screen("NKE", stage="universe", passed=False)

    assert len(decisions_for(conn, run_id=first_id)) == 1
    assert len(decisions_for(conn, ticker="NKE")) == 2


def test_the_jsonl_copy_mirrors_the_database(log_conn):
    conn, tmp = log_conn
    with DecisionLog(conn, mode="paper", as_of=date(2024, 3, 11), log_dir=tmp) as log:
        log.screen("NKE", stage="shock", passed=True)
        log.order(ticker="NKE", side="buy", qty=10, strategy="A")

    import json
    lines = [json.loads(line) for line in
             next(tmp.glob("*.jsonl")).read_text().strip().splitlines()]
    kinds = [line["kind"] for line in lines]
    assert "screen" in kinds
    assert "order" in kinds
    assert kinds[-1] == "run_finished"
