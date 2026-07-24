# Source viability record

**Status: NOT YET MEASURED.** This is a template with the questions filled in
and the answers blank. Fill it in by running:

```bash
.venv/bin/python cli.py check-sources
```

PRD §11 makes this a Phase 0 exit criterion, and §8.1 explains why it comes
before anything is built on top of it: *"Historical depth is the binding
constraint for backtesting Strategy A — evaluate this early, in Phase 0, before
building around it."*

The expensive version of this mistake is building Strategy A, backtesting it,
getting an encouraging result, and only then discovering the news archive starts
in 2022 and the result covers one market regime.

---

## Why the news archive depth is the number that matters

Strategy A's shock trigger (§5.1) needs article volume against a trailing
90-day baseline. Its decay trigger (§5.2) — the actual entry condition — needs
daily counts through a watch window of up to 30 days.

Neither can be computed for a date before the archive starts. So:

> **The earliest date any Strategy A backtest can begin is the archive's start
> date, whatever the price history allows.**

If that date is recent, §13's regime-dependence risk stops being theoretical: a
dip-buying strategy tested only across 2022–2025 has been tested in one kind of
market, and the result will not generalise.

---

## Fill this in

### Market data — Alpaca

| Question | Answer |
|---|---|
| Reachable with the keys in `.env`? | |
| Feed available (IEX free / SIP paid)? | |
| Earliest daily bar for a probe ticker | |
| Does it include delisted securities? | |
| Cost | |

**The delisted-securities question is the important one here.** PRD §8.3: *"If
the dataset only contains companies that still exist, the results are
meaningfully wrong."* If Alpaca will not serve bars for companies that have
since delisted or been acquired, that gap has to be filled from somewhere else
before any backtest is trustworthy — and it will not announce itself, it will
just make results quietly better than reality.

**On the free IEX feed:** IEX is one exchange, so its volumes are a fraction of
consolidated volume. The §4.1 threshold of $50M average daily dollar volume is
written against consolidated figures. Applied to IEX volumes unchanged, it will
reject most of the intended universe. Either use the paid SIP feed or
recalibrate the threshold and write down that you did.

### Fundamentals and filings — SEC EDGAR

| Question | Answer |
|---|---|
| Reachable with your `EDGAR_USER_AGENT`? | |
| Filings returned for a probe company | |
| XBRL facts returned, and how far back | |
| Which §4.2 metrics are actually populated | |
| Restatements visible as separate vintages? | |

Metrics currently mapped are in `EDGAR_TAG_MAP` in `store/metrics.py`. The list
is deliberately short: it grows as each tag is verified against real filings,
rather than being guessed at wholesale. Anything the §4.2 gate needs and this
list lacks will cause that check to fail closed and reject the company — which
is the safe direction, but will look like the gate being over-strict.

### News — provider to be confirmed

| Question | Answer |
|---|---|
| Provider chosen | |
| Reachable? | |
| **Earliest article available** | |
| Coverage of a mega-cap (articles/week) | |
| Coverage of a $10–20bn name (articles/week) | |
| Rate limits | |
| Cost | |

The two coverage rows must be measured separately. `check-sources` probes one
large, heavily covered name, and its coverage says nothing about a company at
the bottom of the §4.1 universe. The §5.1 trigger fires at 3× a trailing
average — on a company averaging one article a week, that threshold is noise,
and the strategy will fire on nothing meaningful.

`ingest/news.py` implements Alpaca's news endpoint as a starting point, since
the account already exists. If its depth or coverage is inadequate, implement
`NewsSource` for another provider; nothing downstream knows which one produced
a row.

### Transcripts — no provider chosen

| Question | Answer |
|---|---|
| Provider evaluated | |
| Five consecutive calls per company available? | |
| Prepared remarks separated from Q&A? | |
| Publication timestamp per transcript? | |
| Archive depth | |
| Cost | |
| Licence permits storage (§8.4)? | |

If none clears the bar, set `strategy_b.b2_tone_shift.enabled: false` in
`config/config.yaml` and proceed with B1 and B3 — exactly as §8.1 provides for.
That is a decision, not a failure. B2 is the only signal that depends on this
source.

The publication-timestamp row is easy to skip and should not be. Transcripts
appear hours to days after a call. Treating them as available at call time is
lookahead, and it is the flattering kind — it would make B2 look better than it
is.

---

## Decisions taken

Record what you chose and why. §13 warns against loosening thresholds on
instinct; the same applies to source choices, and in six months neither of us
will remember the reasoning.

| Date | Decision | Reasoning |
|---|---|---|
| | | |

---

## Verdict

- [ ] Market data verified, delisted coverage confirmed
- [ ] EDGAR verified, §4.2 metrics populated
- [ ] News archive depth measured and recorded
- [ ] News coverage checked on a mid-sized name, not just a mega-cap
- [ ] Transcripts decided — provider chosen, or B2 explicitly descoped
- [ ] Backtest start date chosen, and the reason written down

**Earliest defensible backtest start date: ______**

Phase 1 should not begin until every box above is ticked.
