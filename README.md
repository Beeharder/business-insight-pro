# Multi-Strategy Signal Research Platform

Implementation of PRD v2. **Phase 0 is complete; Phases 1–5 are not started.**

Two signal strategies are planned, running against shared infrastructure so
their contribution can be measured separately:

- **Strategy A — Sentiment Overreaction.** Buy large caps that sold off on
  non-fundamental news, entering when the news cycle visibly cools.
- **Strategy B — Change Detection.** Buy on material changes in a company's own
  disclosure language relative to its own history.

Paper trading only. Real money requires the PRD §12.3 gate.

New here? Read [RUNBOOK.md](RUNBOOK.md) — it assumes no prior terminal
experience and gets you to a working demo in three commands.

---

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

.venv/bin/python cli.py init-db      # create the local database
.venv/bin/python cli.py seed-demo    # synthetic data — no API keys needed
.venv/bin/python cli.py verify       # prove the backtester is correct
.venv/bin/python -m pytest           # 131 tests, about a minute
```

On Windows, swap `.venv/bin/python` for `.venv\Scripts\python` throughout —
[RUNBOOK.md](RUNBOOK.md) gives both forms for every command.

`verify` must print `PASS — the engine reproduces buy-and-hold exactly.`
That is the Phase 0 exit criterion. Until it passes, no backtest result from
this system is worth reading.

---

## What Phase 0 delivers

| PRD requirement | Where it lives |
|---|---|
| Point-in-time data access (§8.3) | `store/asof.py` |
| Delisted securities retained (§8.3) | `securities` table + `test_point_in_time.py` |
| Filings indexed by filing date (§8.3) | `store/schema.sql`, `ingest/filings.py` |
| Fundamentals as originally reported (§8.3) | `AsOf.fundamental(vintage=...)` |
| EXRO rejected by the gate (§8.3) | `tests/test_survivability.py::test_exro_is_rejected` |
| Backtester reproduces buy-and-hold (§11) | `backtest/benchmark.py`, `cli.py verify` |
| Universe and survivability screens (§4) | `screens/` |
| Full decision traces (§8.4) | `store/decision_log.py` |
| Failure simulation (§8.5) | `tests/test_failure_modes.py` |
| Source viability check (§8.1, §11) | `ingest/check_sources.py` |
| Plain-language runbook (§9) | `RUNBOOK.md` |
| Secrets never committed (§9) | `.gitignore`, `.env.example` |

---

## Repository layout

Follows PRD §8.2, with one addition:

```
/config      thresholds (committed) and .env loading (never committed)
/store       the database, its schema, and the point-in-time reader   <- added
/ingest      market data, filings, news, transcripts, viability check
/screens     deterministic triggers (§4)
/backtest    point-in-time engine, benchmark, metrics
/logs        decision traces (git-ignored)
/tests
/data        local DuckDB file (git-ignored)
/docs
```

`/store` is not in §8.2 because the PRD describes the point-in-time function as
a requirement rather than a component. In practice it needs to own the schema
and be the only path to the data, which makes it a package. `/llm`,
`/strategies`, `/risk`, `/execution` and `/reporting` arrive with the phases
that need them.

---

## The one design decision worth knowing

**Nothing reads the database directly.** Everything goes through an `AsOf`
object bound to a single date:

```python
view = AsOf(conn, date(2023, 6, 15))
view.close("AAPL")                       # the 15 June close
view.fundamental("AAPL", "revenue")      # last figure FILED by 15 June
view.close("AAPL", date(2024, 1, 2))     # raises FutureDataError
```

`AsOf` cannot return anything stamped later than the date it is bound to. That
turns lookahead bias from something you have to remember to avoid into
something the code will not do.

The PRD calls point-in-time correctness "the single most important technical
requirement" (§8.3), and the leaks it warns about are all handled explicitly:

- **Survivorship.** Delisted companies stay in the universe for dates before
  they delisted. A 2015 backtest can buy a company that died in 2017 — as it
  must, or the losers have been quietly deleted from history.
- **Restatements.** A correction is a new row, never an overwrite. Both the
  originally reported figure and the amended one survive, and you choose which
  you are asking for.
- **Filing lag.** A quarter ending 31 March becomes visible when the 10-Q is
  filed in May, not on 31 March.
- **Split adjustment.** Prices are stored raw. A split is applied to history
  only from the date it went ex, so an announcement cannot retroactively bend a
  chart the market had not yet redrawn.
- **News backfill.** Articles are counted from when they became *available*, not
  when they were written, so an archive that fills in a story weeks later cannot
  create a news spike on a day the market saw nothing.

There is a test for each, in `tests/test_point_in_time.py`.

---

## Commands

| Command | What it does |
|---|---|
| `cli.py init-db` | Create an empty local database |
| `cli.py seed-demo` | Fill it with synthetic data (no keys needed) |
| `cli.py check-sources` | Phase 0 viability check on the real sources |
| `cli.py verify` | Prove the engine reproduces buy-and-hold exactly |
| `cli.py backtest` | Run a backtest |
| `cli.py screen` | Run the §4 screens on one ticker, one date |
| `cli.py logs` | Decision trace for a ticker and date |
| `cli.py status` | Database contents, data freshness, recent runs |

---

## Where Phase 0 stops

Two things the PRD asks for in Phase 0 could not be finished here, both because
they need live network access with your credentials:

1. **Source viability is unmeasured.** `cli.py check-sources` is written and
   ready, but has not been run against the real APIs. Until it has, the news
   archive depth is unknown — and that number is the hard floor on how far back
   Strategy A can ever be backtested (§8.1). Run it and record the results in
   [docs/SOURCE_VIABILITY.md](docs/SOURCE_VIABILITY.md) before starting Phase 1.

2. **No adapters have touched a live API.** Parsing is tested against recorded
   response shapes, so the point-in-time logic is verified, but the first real
   call may still surface a field that differs from expectation.

The backtester's correctness does not depend on either. It is proven against
synthetic data whose properties are known exactly, which is a stronger test of
the engine than real data would be — with real data you cannot tell an engine
bug from a data quirk.

**No transcript provider is chosen**, which the PRD explicitly leaves open
(§8.1). Strategy B2 is descoped until one is; B1 and B3 do not need it. See
`ingest/transcripts.py` for what a provider has to supply to be worth using.

---

## Next: Phase 1

Strategy A, rules only, no model. The shock trigger (§5.1), the decay trigger
(§5.2), and the disqualifiers (§5.3), backtested and benchmarked.

Phase 1 exists to be the measurement baseline for Phase 2. Without it there is
no way to know whether the LLM layer contributes anything — which is the
central question this project exists to answer (§11).
