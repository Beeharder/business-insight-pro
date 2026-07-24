"""The survivability gate (PRD §4.2).

Runs before any LLM call, on every candidate, in both strategies. Entirely
deterministic and computed from filings — no judgment, no model, no ambiguity.

Its job is narrow: keep out companies that might not be there in six months.
PRD §4.1 records why it exists — the owner's one total loss was a small-cap
that delisted. A cheap stock that goes to zero is not an overreaction to buy.

Everything here **fails closed**. A check that cannot be evaluated because the
data is missing is a failed check, not a skipped one. That will reject some
perfectly sound companies when coverage is patchy, and that is the intended
trade: §9 says never guess and proceed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from store import metrics as M
from store.asof import AsOf

ANNUAL_AND_QUARTERLY = ("10-K", "10-Q")


@dataclass
class Check:
    name: str
    passed: bool
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class GateResult:
    ticker: str
    as_of: date
    passed: bool
    checks: list[Check]

    @property
    def failures(self) -> list[Check]:
        return [check for check in self.checks if not check.passed]

    @property
    def failure_summary(self) -> str:
        return "; ".join(f"{check.name}: {check.reason}" for check in self.failures)

    def as_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "as_of": self.as_of,
            "passed": self.passed,
            "checks": {c.name: {"passed": c.passed, "reason": c.reason, **c.detail}
                       for c in self.checks},
        }


def check_cash_flow_or_runway(view: AsOf, ticker: str, min_runway_months: int) -> Check:
    """Positive trailing free cash flow, **or** at least 18 months of runway.

    Free cash flow here is trailing-twelve-month operating cash flow minus
    capital expenditure. If it is positive the company funds itself and the
    runway question does not arise. If it is negative, cash on hand has to cover
    the burn for ``min_runway_months``.
    """
    ocf = view.ttm(ticker, M.OPERATING_CASH_FLOW)
    capex = view.ttm(ticker, M.CAPEX)
    if ocf is None or capex is None:
        return Check("cash_flow_or_runway", False,
                     "no four full quarters of cash-flow data visible on this date",
                     {"ocf_ttm": ocf, "capex_ttm": capex})

    fcf = ocf - abs(capex)
    if fcf > 0:
        return Check("cash_flow_or_runway", True, "positive trailing free cash flow",
                     {"fcf_ttm": fcf})

    cash = view.fundamental(ticker, M.CASH_AND_EQUIVALENTS)
    if cash is None:
        return Check("cash_flow_or_runway", False,
                     "negative free cash flow and no cash balance to measure runway against",
                     {"fcf_ttm": fcf})

    monthly_burn = abs(fcf) / 12.0
    runway_months = cash / monthly_burn if monthly_burn else float("inf")
    passed = runway_months >= min_runway_months
    return Check(
        "cash_flow_or_runway", passed,
        f"{runway_months:.1f} months of runway against a {min_runway_months}-month requirement",
        {"fcf_ttm": fcf, "cash": cash, "runway_months": runway_months},
    )


def check_going_concern(view: AsOf, ticker: str) -> Check:
    """No going-concern language in the latest 10-K or 10-Q.

    Auditors do not raise substantial doubt about a company's ability to
    continue lightly. When they do, no amount of price weakness makes it a
    buy — this is the §4.2 check that would have caught the owner's loss.
    """
    latest = [view.latest_filing(ticker, form) for form in ANNUAL_AND_QUARTERLY]
    seen = [f for f in latest if f]
    if not seen:
        return Check("going_concern", False, "no 10-K or 10-Q visible on this date")

    flagged = [f for f in seen if f.get("going_concern_flag")]
    if flagged:
        import pandas as pd

        forms = ", ".join(
            f"{f['form_type']} filed {pd.Timestamp(f['filing_date']).date()}" for f in flagged
        )
        return Check("going_concern", False, f"going-concern language in {forms}",
                     {"filings": forms})
    return Check("going_concern", True, "no going-concern language in the latest filings")


def check_debt_coverage(view: AsOf, ticker: str) -> Check:
    """Debt maturing within 24 months covered by cash plus operating cash flow.

    A company does not have to be profitable to survive; it has to be able to
    pay what comes due. Missing the maturity figure is treated as a pass on the
    narrow reading that nothing is disclosed as due — but only when cash and
    operating cash flow are both visible, so it can never mask a company with no
    financial data at all.
    """
    due = view.fundamental(ticker, M.DEBT_DUE_WITHIN_24M)
    cash = view.fundamental(ticker, M.CASH_AND_EQUIVALENTS)
    ocf = view.ttm(ticker, M.OPERATING_CASH_FLOW)

    if cash is None or ocf is None:
        return Check("debt_coverage", False,
                     "cannot see cash or operating cash flow on this date",
                     {"cash": cash, "ocf_ttm": ocf})
    if due is None:
        return Check("debt_coverage", True, "no near-term maturities disclosed",
                     {"cash": cash, "ocf_ttm": ocf})

    available = cash + max(ocf, 0.0)
    passed = available >= due
    return Check(
        "debt_coverage", passed,
        f"{available:,.0f} of cash and operating cash flow against {due:,.0f} due within 24 months",
        {"debt_due_24m": due, "cash": cash, "ocf_ttm": ocf, "coverage": available},
    )


def check_not_in_default(view: AsOf, ticker: str, lookback_months: int = 24) -> Check:
    """No disclosed default or covenant breach in recent filings."""
    cutoff = view.as_of - timedelta(days=int(lookback_months * 30.44))
    filings = view.filings(ticker, limit=40)
    if filings.empty:
        return Check("not_in_default", False, "no filings visible on this date")

    import pandas as pd

    recent = filings[pd.to_datetime(filings["filing_date"]).dt.date >= cutoff]
    breaches = recent[recent["default_or_covenant_flag"] == True]  # noqa: E712
    if not breaches.empty:
        when = breaches.iloc[0]["filing_date"]
        return Check("not_in_default", False,
                     f"default or covenant breach disclosed in a filing dated {when}")
    return Check("not_in_default", True, "no default or covenant breach disclosed")


def check_no_delisting_notice(view: AsOf, ticker: str) -> Check:
    """No active delisting notice.

    Checked two ways, because either source alone can be silent: the reference
    record for the security, and any recent filing carrying the flag.
    """
    security = view.security(ticker)
    if security:
        notice = security.get("delisting_notice_date")
        if notice is not None and not _is_missing(notice):
            import pandas as pd

            notice_date = pd.Timestamp(notice).date()
            if notice_date <= view.as_of:
                return Check("no_delisting_notice", False,
                             f"delisting notice dated {notice_date}",
                             {"delisting_notice_date": notice_date})

    filings = view.filings(ticker, limit=40)
    if not filings.empty:
        flagged = filings[filings["delisting_notice_flag"] == True]  # noqa: E712
        if not flagged.empty:
            return Check("no_delisting_notice", False,
                         f"delisting notice in a filing dated {flagged.iloc[0]['filing_date']}")

    if view.is_delisted(ticker):
        return Check("no_delisting_notice", False, "security is already delisted")

    return Check("no_delisting_notice", True, "no delisting notice on file")


def _is_missing(value) -> bool:
    import pandas as pd

    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return value is None


def survivability_gate(
    view: AsOf,
    ticker: str,
    *,
    min_runway_months: int = 18,
    debt_lookback_months: int = 24,
) -> GateResult:
    """Run all five §4.2 checks. Every one must pass.

    All checks run even after the first failure, so the decision log records the
    full picture rather than just whichever check happened to be evaluated
    first — that matters when reviewing rejections months later (§12.1).
    """
    checks = [
        check_cash_flow_or_runway(view, ticker, min_runway_months),
        check_going_concern(view, ticker),
        check_debt_coverage(view, ticker),
        check_not_in_default(view, ticker, debt_lookback_months),
        check_no_delisting_notice(view, ticker),
    ]
    return GateResult(
        ticker=ticker,
        as_of=view.as_of,
        passed=all(check.passed for check in checks),
        checks=checks,
    )
