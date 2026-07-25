# Runbook

Written for someone who is not a developer. No prior knowledge of terminals is
assumed. If a step does not do what this page says it will, that is a bug in
the page — say so and it gets fixed.

**Current state: Phase 0.** The data pipeline and the backtester exist. No
strategy exists yet, nothing trades, and nothing connects to a broker. See
"What this can and cannot do yet" at the bottom.

---

## Before you start: which set of commands to use

Every command below is given twice, because Windows spells things differently
from Mac and Linux. Use the row that matches your machine and ignore the other.

|  | Windows | Mac / Linux |
|---|---|---|
| Terminal app | **PowerShell** (press Start, type "PowerShell") | **Terminal** |
| Run Python | `.venv\Scripts\python` | `.venv/bin/python` |
| Copy a file | `copy` | `cp` |
| Delete a file | `del` | `rm` |

The only thing you have to get right is the `.venv\Scripts\` versus `.venv/bin/`
part. Everything after it is identical.

**If you are working in a cloud session on claude.ai, use the Mac / Linux
commands** — the cloud machine runs Linux, regardless of what you are typing on.

---

## 1. One-time setup

You need Python 3.11 or newer.

**Windows.** Install from python.org, **not** the Microsoft Store — the Store
version is sandboxed in ways that cause confusing failures later. On the very
first screen of the installer, tick **"Add python.exe to PATH"** before clicking
Install. It is easy to miss and skipping it is the single most common way this
whole process goes wrong.

To check it worked, open PowerShell and type:

```powershell
python --version
```

**Mac / Linux.** Type:

```bash
python3 --version
```

Either way, if that prints something lower than 3.11, or an error, fix that
before going further.

Now set the project up. In your terminal, navigate to the project folder, then:

**Windows**
```powershell
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
```

**Mac / Linux**
```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

**What that did.** The first line made a private folder called `.venv` holding
this project's own copy of Python and its libraries, so nothing here can break
anything else on your computer. The second installed what the project needs.

From here on, every command starts with `.venv\Scripts\python` (Windows) or
`.venv/bin/python` (Mac/Linux). That is how you say "use *this project's*
Python". If you ever see `ModuleNotFoundError`, it is almost always because that
prefix got left off the front.

---

## 2. Setting up credentials

Keys live in a file called `.env`, which is deliberately excluded from git and
will never be committed. Create it from the template:

**Windows**
```powershell
copy .env.example .env
```

**Mac / Linux**
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

### If you are running in a cloud session instead

Cloud sessions are wiped between runs, so a `.env` file you create there will
not survive. Put your credentials in the **environment variables** box in the
environment's settings dialog instead — the code reads real environment
variables in preference to the `.env` file, so no other change is needed.

You will also need to allow the data websites through, since cloud environments
block them by default. In the same settings dialog set **Network access** to
**Custom** and add:

```
*.sec.gov
*.alpaca.markets
```

Leave "Also include default list of common package managers" ticked, or GitHub
and the Python package sites get cut off too. Settings only apply to **new**
sessions, so start a fresh one afterwards.

---

## 3. Starting the system

There is no long-running system to start yet. Phase 0 is a set of commands you
run one at a time.

### See it work, with no keys

**Windows**
```powershell
.venv\Scripts\python cli.py init-db
.venv\Scripts\python cli.py seed-demo
.venv\Scripts\python cli.py verify
```

**Mac / Linux**
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

```
.venv\Scripts\python cli.py check-sources      (Windows)
.venv/bin/python cli.py check-sources          (Mac / Linux)
```

This is the other half of Phase 0. It reports whether each source answers, and
**how far back its history goes**. Write the answers into
`docs/SOURCE_VIABILITY.md` — the news archive depth in particular sets a hard
floor on how far back Strategy A can ever be tested.

### Look at one company on one day

```
.venv\Scripts\python cli.py screen --ticker EXRO --date 2023-08-01   (Windows)
.venv/bin/python cli.py screen --ticker EXRO --date 2023-08-01       (Mac / Linux)
```

Shows every size, liquidity and survivability check with its reasoning. Useful
for building a feel for what the rules actually do.

---

## 4. Stopping the system

Press `Ctrl+C`. Every command is either read-only or writes to the local
database file, so interrupting one cannot corrupt anything or place an order.

To throw everything away and start clean:

**Windows**
```powershell
del data\market.duckdb
.venv\Scripts\python cli.py init-db
```

**Mac / Linux**
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

```
.venv\Scripts\python cli.py logs --ticker NKE --date 2024-03-11   (Windows)
.venv/bin/python cli.py logs --ticker NKE --date 2024-03-11       (Mac / Linux)
```

Shows every screen that ran on that company that day, whether it passed, and
the numbers behind the verdict.

**The plain-text copy** — one file per run in `logs/`, written line by line as
things happen. To list them newest first:

**Windows**
```powershell
dir logs /O-D
```

**Mac / Linux**
```bash
ls -lt logs/ | head
```

This copy exists because the database copy is only useful if the program got far
enough to save its work. If a run dies halfway, the text file is what tells you
where.

To see what the system has and how fresh it is:

```
.venv\Scripts\python cli.py status   (Windows)
.venv/bin/python cli.py status       (Mac / Linux)
```

---

## 6. The four most likely failure messages

### Windows: "python is not recognized as the name of a cmdlet"

**Means:** Windows cannot find Python. Almost always the "Add python.exe to
PATH" box was left unticked during installation.

**Fix:** re-run the python.org installer, choose **Modify**, and make sure that
box is ticked. Then close PowerShell entirely and open a new one — an already
open terminal will not notice the change.

If you installed from the Microsoft Store, uninstall it and use the python.org
installer instead. The Store version behaves differently in ways that cause
problems later.

### "ModuleNotFoundError: No module named 'duckdb'"

**Means:** the command ran with your computer's Python instead of the project's.

**Fix:** start the command with `.venv\Scripts\python` on Windows, or
`.venv/bin/python` on Mac and Linux — not plain `python`. If it still fails,
re-run the install step in section 1.

### "ERROR: EDGAR_USER_AGENT is not set, and it is needed to..."

**Means:** a credential is missing from `.env`. The message always names which
one and what it is for.

**Fix:** open `.env` and fill in the named value. If the file does not exist,
create it from the template as shown in section 2.

This is the system working as intended. It refuses to run half-configured
rather than guessing and carrying on.

### "GET https://... failed after 4 attempts"

**Means:** a data source did not answer, after four tries with increasing pauses
between them.

**Fix:** usually nothing — try again in ten minutes. If it persists, check the
provider's status page. If it says `401` or `403`, the key is wrong or expired
rather than the service being down; retrying will not help. In a cloud session,
a `403` usually means the website is not on the environment's allowed list —
see the end of section 2.

The system stops rather than continuing with partial data. That matters more
than it sounds: an empty news result would look exactly like a quiet news day,
and Strategy A's entry signal is *falling news volume*. Silence must never be
mistaken for information.

---

## 7. Verifying it actually ran today

Once the daily cycle exists in Phase 4, the check will be: **a digest email
arrives every trading day, even when nothing happened.** Silence is the alert.
No email means something broke.

Today, in Phase 0, the equivalent check is the `status` command from section 5.
Look at "Recent runs". A healthy run shows status `ok` and a `finished_at` time.
A run showing `failed` has its error recorded next to it. A run stuck on
`running` from hours ago means the process was killed — check the newest file
in `logs/` for the last thing it managed to write.

---

## 8. Running the tests

```
.venv\Scripts\python -m pytest   (Windows)
.venv/bin/python -m pytest       (Mac / Linux)
```

Takes about a minute. Everything should pass. Run this after any change,
including ones that look harmless — particularly changes to anything in
`store/`, which is where a mistake does the most damage and shows the fewest
symptoms.

If a test fails, its name says what broke. `test_exro_is_rejected` failing means
the safety gate stopped catching a company the PRD names by hand as one it must
reject.

The tests in `tests/test_cross_platform.py` exist specifically to catch bugs
that only appear on Windows. They work by forcing the same conditions on
whatever machine they run on, so a problem that would only show up on your
laptop still gets caught here.

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
