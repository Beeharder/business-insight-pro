"""Phase 0 source viability check (PRD §8.1, §11).

Answers the questions Phase 0 exists to answer, before anything is built on top
of the answers:

* Can we reach each source at all, with the credentials in ``.env``?
* How far back does the price history go?
* **How far back does the news archive go?** This is the binding constraint on
  backtesting Strategy A (§8.1). If the archive starts in 2022, no amount of
  work makes a 2015 backtest possible.
* Are transcripts available and affordable? If not, B2 is descoped (§8.1) and
  B1/B3 proceed — a decision, not a failure.

Run it with::

    python cli.py check-sources

It only reads. Nothing here writes to the store or places an order.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable

from config import secret
from config.loader import MissingSecret

from .base import IngestError, SourceUnavailable

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skipped"

# A liquid, long-listed name used as the probe target. Anything the archive
# does not have for this ticker, it will not have for a mid-cap either.
PROBE_TICKER = "AAPL"
PROBE_CIK = 320193


@dataclass
class SourceCheck:
    name: str
    status: str
    detail: str
    blocking: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def icon(self) -> str:
        return {OK: "PASS", WARN: "WARN", FAIL: "FAIL", SKIP: "SKIP"}[self.status]


def _run(name: str, blocking: bool, probe: Callable[[], SourceCheck]) -> SourceCheck:
    """Run one probe, turning any exception into a reportable result.

    The point of this command is to survey what works. One dead source must not
    stop the survey — you want the whole picture in a single run.
    """
    try:
        return probe()
    except (SourceUnavailable, MissingSecret) as exc:
        # Not configured is a different thing from broken, and the remedy is
        # different too, so it is reported as skipped rather than failed.
        return SourceCheck(name, SKIP, str(exc), blocking)
    except IngestError as exc:
        return SourceCheck(name, FAIL, str(exc), blocking)
    except Exception as exc:                     # noqa: BLE001 - survey must continue
        return SourceCheck(
            name, FAIL, f"unexpected error: {exc}", blocking,
            notes=[traceback.format_exc(limit=3)],
        )


# -- individual probes -------------------------------------------------------


def check_market_data() -> SourceCheck:
    from . import market

    client = market.build_client(timeout=20)
    recent_end = date.today() - timedelta(days=1)
    recent = market.fetch_bars(client, [PROBE_TICKER], recent_end - timedelta(days=10), recent_end)
    if not recent.get(PROBE_TICKER):
        return SourceCheck(
            "Alpaca market data", FAIL,
            f"connected, but returned no recent bars for {PROBE_TICKER}", blocking=True,
        )

    # How deep does the history go? Probe a decade back rather than paging
    # through everything, which would be slow and pointless.
    deep_start = date.today() - timedelta(days=365 * 10)
    deep = market.fetch_bars(client, [PROBE_TICKER], deep_start, deep_start + timedelta(days=30))
    depth = "10+ years" if deep.get(PROBE_TICKER) else "less than 10 years — probe further"

    return SourceCheck(
        "Alpaca market data", OK,
        f"{len(recent[PROBE_TICKER])} recent bars for {PROBE_TICKER}; history depth: {depth}",
        notes=[
            "Free tier serves the IEX feed only. IEX volume is a fraction of "
            "consolidated volume, so the §4.1 $50M dollar-volume threshold must "
            "be calibrated against whichever feed you use.",
        ],
    )


def check_corporate_actions() -> SourceCheck:
    from . import market

    client = market.build_client(timeout=20)
    end = date.today()
    actions = market.fetch_corporate_actions(client, [PROBE_TICKER], end - timedelta(days=365 * 5), end)
    total = sum(len(items) for items in actions.values())
    if total == 0:
        return SourceCheck(
            "Alpaca corporate actions", WARN,
            f"reachable, but no actions returned for {PROBE_TICKER} over five years — "
            "verify by hand, since this name pays dividends",
        )
    return SourceCheck("Alpaca corporate actions", OK,
                       f"{total} actions for {PROBE_TICKER} over five years")


def check_edgar() -> SourceCheck:
    from . import filings

    client = filings.build_client(timeout=25)
    submissions = filings.fetch_submissions(client, PROBE_CIK)
    rows = filings.parse_submissions(submissions, PROBE_TICKER, PROBE_CIK)
    annuals = [r for r in rows if r["form_type"] == "10-K"]

    facts = filings.parse_company_facts(filings.fetch_company_facts(client, PROBE_CIK), PROBE_TICKER)
    metrics_found = sorted({row["metric"] for row in facts})
    oldest = min((row["filing_date"] for row in facts), default=None)

    status = OK if annuals and metrics_found else WARN
    return SourceCheck(
        "SEC EDGAR", status,
        f"{len(rows)} filings ({len(annuals)} 10-Ks); "
        f"{len(facts)} facts covering {len(metrics_found)} metrics back to {oldest}",
        blocking=True,
        notes=[
            f"Metrics mapped so far: {', '.join(metrics_found) or 'none'}. "
            "Anything the §4.2 gate needs and this list lacks must be added to "
            "EDGAR_TAG_MAP in store/metrics.py and re-checked.",
        ],
    )


def check_news() -> SourceCheck:
    from .news import AlpacaNews

    source = AlpacaNews()
    end = date.today() - timedelta(days=1)
    recent = source.fetch([PROBE_TICKER], end - timedelta(days=7), end)
    earliest = source.earliest_available(PROBE_TICKER)

    if not recent:
        return SourceCheck("News archive", FAIL,
                           f"no articles for {PROBE_TICKER} in the last week", blocking=True)

    if earliest is None:
        detail = f"{len(recent)} articles in the last week; archive depth could not be determined"
        status = WARN
    else:
        years = (date.today() - earliest).days / 365.25
        detail = (f"{len(recent)} articles in the last week; archive reaches back to "
                  f"{earliest} ({years:.1f} years)")
        status = OK if years >= 5 else WARN

    return SourceCheck(
        "News archive", status, detail, blocking=True,
        notes=[
            "This depth is the hard floor on any Strategy A backtest (§8.1). A "
            "backtest cannot start before this date, whatever the price history "
            "allows.",
            "Coverage of one mega-cap says little about a $10-20bn name. Before "
            "trusting the §5.1 3x-volume threshold, re-run this probe against "
            "several mid-sized names from the real universe.",
        ],
    )


def check_transcripts() -> SourceCheck:
    provider = secret("TRANSCRIPTS_PROVIDER")
    if not provider:
        return SourceCheck(
            "Transcripts", SKIP,
            "no provider configured — Strategy B2 is descoped until one is chosen (§8.1)",
            notes=[
                "B1 and B3 do not need transcripts and can proceed regardless.",
                "See the module docstring in ingest/transcripts.py for what a "
                "provider has to supply before B2 is worth building.",
            ],
        )
    return SourceCheck(
        "Transcripts", WARN,
        f"TRANSCRIPTS_PROVIDER is set to '{provider}', but no adapter is implemented for it yet",
    )


def check_llm() -> SourceCheck:
    """Not needed until Phase 2 — reported so its absence is a known state
    rather than a surprise later."""
    if not secret("ANTHROPIC_API_KEY"):
        return SourceCheck("Anthropic API", SKIP,
                           "no key set; not required until Phase 2")
    return SourceCheck("Anthropic API", OK, "key present (not exercised by this check)")


# -- the survey --------------------------------------------------------------


def run_all() -> list[SourceCheck]:
    return [
        _run("Alpaca market data", True, check_market_data),
        _run("Alpaca corporate actions", False, check_corporate_actions),
        _run("SEC EDGAR", True, check_edgar),
        _run("News archive", True, check_news),
        _run("Transcripts", False, check_transcripts),
        _run("Anthropic API", False, check_llm),
    ]


def format_report(checks: list[SourceCheck]) -> str:
    lines = ["", "Phase 0 source viability check", "=" * 60, ""]
    for check in checks:
        lines.append(f"[{check.icon}] {check.name}")
        lines.append(f"       {check.detail}")
        for note in check.notes:
            lines.append(f"       note: {note}")
        lines.append("")

    blocking_failures = [c for c in checks if c.blocking and c.status in (FAIL, SKIP)]
    lines.append("-" * 60)
    if blocking_failures:
        lines.append("Phase 0 is NOT satisfied. Blocking problems:")
        for check in blocking_failures:
            lines.append(f"  - {check.name}: {check.detail}")
        lines.append("")
        lines.append("Fix these before backtesting anything. A backtest built on a "
                     "source you have not verified is a waste of the time it takes to run.")
    else:
        lines.append("All blocking sources verified. Record the archive depths in "
                     "docs/SOURCE_VIABILITY.md, then Phase 1 can start.")
    lines.append("")
    return "\n".join(lines)
