# Runbook

Written for someone who is not a developer. No prior knowledge of terminals is
assumed. If a step does not do what this page says it will, that is a bug in
the page — say so and it gets fixed.

**Current state: Phase 0.** The data pipeline and the backtester exist. No
strategy exists yet, nothing trades, and nothing connects to a broker. See
"What this can and cannot do yet" at the bottom.

---

## 1. One-time setup

You need Python 3.11 or newer. To check, open a terminal and type:

```bash
python3 --version
```

If that prints something lower than 3.11, or an error, install Python from
python.org first.

Then, from the project folder:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

**What that did.** The first line made a private folder called `.venv` holding
this project's own copy of Python and its libraries, so nothing here can break
anything else on your computer. The second installed what the project needs.

From here on, every command starts with `.venv/bin/python`. That is how you say
"use *this project's* Python". If you ever see `ModuleNotFoundError`, it is
almost always because `.venv/bin/` got left off the front.

---

## 2. Setting up credentials

Keys live in a file called `.env`, which is deliberately excluded from git and
will never be committed. Create it from the template:

```bash
cp .env.example .env
```

Then open `.env` in any text editor and fill in the values. `.env.example`
explains each one and where to get it.

**Nothing in Phase 0 needs credentials except `check-sources` and the live
ingest commands.** You can run the demo and the whole test suite without any
keys at all.

Two things worth knowing:

- Use **paper** keys from Alpaca, not live ones. Paper keys cannot place a real
  order even by accident.
- `EDGAR_USER_AGENT` must contain a real contact email, like
  `Jane Smith jane@example.com`. The SEC blocks anonymous clients, and the block
  is not obvious when it happens.

If you ever think a key has leaked, revoke it at the provider first and worry
about the file second.

---

## 3. Starting the system

There is no long-running system to start yet. Phase 0 is a set of commands you
run one at a time.

### See it work, with no keys

```bash
.venv/bin/python cli.py init-db
.venv/bin/python cli.py seed-demo
.venv/bin/python cli.py verify
```

The third command should end with:

```
PASS — the engine reproduces buy-and-hold exactly.
```

That line is the Phase 0 exit criterion from the PRD. It means the backtester
buys, holds, handles a stock split and a dividend, and arrives at exactly the
number three independent calculations say it should. Until it says PASS, no
backtest result from this system means anything.

**The demo data is invented.** It exists to exercise the plumbing. No number
computed from it says anything about any strategy.

### Check the real data sources

```bash
.venv/bin/python cli.py check-sources
```

This is the other half of Phase 0. It reports whether each source answers, and
**how far back its history goes**. Write the answers into
`docs/SOURCE_VIABILITY.md` — the news archive depth in particular sets a hard
floor on how far back Strategy A can ever be tested.

### Look at one company on one day

```bash
.venv/bin/python cli.py screen --ticker EXRO --date 2023-08-01
```

Shows every size, liquidity and survivability check with its reasoning. Useful
for building a feel for what the rules actually do.

---

## 4. Stopping the system

Press `Ctrl+C`. Every command is either read-only or writes to the local
database file, so interrupting one cannot corrupt anything or place an order.

To throw everything away and start clean:

```bash
rm data/market.duckdb
.venv/bin/python cli.py init-db
```

You lose only downloaded data, which can be re-fetched. Nothing irreplaceable
lives in that file.

---

## 5. Reading the logs

Two copies of every decision are kept, on purpose.

**The database copy** — queryable, and what the CLI reads:

```bash
.venv/bin/python cli.py logs --ticker NKE --date 2024-03-11
```

Shows every screen that ran on that company that day, whether it passed, and
the numbers behind the verdict.

**The plain-text copy** — one file per run in `logs/`, written line by line as
things happen:

```bash
ls -lt logs/ | head
```

The newest file is the most recent run. This copy exists because the database
copy is only useful if the program got far enough to save its work. If a run
dies halfway, the text file is what tells you where.

To see what the system has and how fresh it is:

```bash
.venv/bin/python cli.py status
```

---

## 6. The three most likely failure messages

### "ModuleNotFoundError: No module named 'duckdb'"

**Means:** the command ran with your computer's Python instead of the project's.

**Fix:** start the command with `.venv/bin/python`, not `python`. If it still
fails, re-run the install step in section 1.

### "ERROR: EDGAR_USER_AGENT is not set, and it is needed to..."

**Means:** a credential is missing from `.env`. The message always names which
one and what it is for.

**Fix:** open `.env` and fill in the named value. If the file does not exist,
`cp .env.example .env` first.

This is the system working as intended. It refuses to run half-configured
rather than guessing and carrying on.

### "GET https://... failed after 4 attempts"

**Means:** a data source did not answer, after four tries with increasing pauses
between them.

**Fix:** usually nothing — try again in ten minutes. If it persists, check the
provider's status page. If it says `401` or `403`, the key is wrong or expired
rather than the service being down; retrying will not help.

The system stops rather than continuing with partial data. That matters more
than it sounds: an empty news result would look exactly like a quiet news day,
and Strategy A's entry signal is *falling news volume*. Silence must never be
mistaken for information.

---

## 7. Verifying it actually ran today

Once the daily cycle exists in Phase 4, the check will be: **a digest email
arrives every trading day, even when nothing happened.** Silence is the alert.
No email means something broke.

Today, in Phase 0, the equivalent check is:

```bash
.venv/bin/python cli.py status
```

Look at "Recent runs". A healthy run shows status `ok` and a `finished_at` time.
A run showing `failed` has its error recorded next to it. A run stuck on
`running` from hours ago means the process was killed — check the newest file
in `logs/` for the last thing it managed to write.

---

## 8. Running the tests

```bash
.venv/bin/python -m pytest
```

Takes about 40 seconds. Everything should pass. Run this after any change,
including ones that look harmless — particularly changes to anything in
`store/`, which is where a mistake does the most damage and shows the fewest
symptoms.

If a test fails, its name says what broke. `test_exro_is_rejected` failing means
the safety gate stopped catching a company the PRD names by hand as one it must
reject.

---

## 9. What this can and cannot do yet

**It can:**
- Store prices, filings, fundamentals and news with an honest record of when
  each fact became knowable
- Answer "what did we know on this date" without leaking later information
- Run a backtest that provably reproduces buy-and-hold exactly
- Apply the universe and survivability rules to any company on any date
- Record every decision so it can be reconstructed later

**It cannot yet:**
- Detect a selloff, or anything else Strategy A does — that is Phase 1
- Call a language model — Phase 2
- Compare filings or earnings calls — Phase 3
- Place an order, paper or otherwise — Phase 4
- Send a digest email — Phase 4

The phases are in the PRD (§11) and are deliberately in this order. Phase 1
builds Strategy A **without** the model, so that Phase 2 has something to be
measured against. Skipping it would leave the project unable to answer the one
question it exists to answer: does the model contribute anything at all.
