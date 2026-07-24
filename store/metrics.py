"""The vocabulary of financial metric names used in the ``fundamentals`` table.

Every ingest adapter maps its source's naming onto these constants, so the
screens never need to know whether a number came from EDGAR's XBRL tags or a
commercial API. Add to this list rather than inventing a name at a call site —
a typo'd metric name silently returns ``None``, which a fail-closed gate then
turns into a rejection you will spend an afternoon explaining.
"""

# Cash flow — the §4.2 free-cash-flow test
OPERATING_CASH_FLOW = "operating_cash_flow"
CAPEX = "capital_expenditures"           # positive number; FCF subtracts it
FREE_CASH_FLOW = "free_cash_flow"

# Balance sheet — runway and debt coverage
CASH_AND_EQUIVALENTS = "cash_and_equivalents"
SHORT_TERM_INVESTMENTS = "short_term_investments"
TOTAL_DEBT = "total_debt"
DEBT_DUE_WITHIN_24M = "debt_due_within_24m"

# Income statement — size and the §5.3 revenue-concentration disqualifier
REVENUE = "revenue"
NET_INCOME = "net_income"

# Market data derived — the §4.1 universe test
MARKET_CAP = "market_cap"
SHARES_OUTSTANDING = "shares_outstanding"

# Mapping from EDGAR XBRL us-gaap tags to the names above. Incomplete by
# design: it grows as the ingest layer is verified tag by tag against real
# filings, rather than being guessed at wholesale.
EDGAR_TAG_MAP: dict[str, str] = {
    "NetCashProvidedByUsedInOperatingActivities": OPERATING_CASH_FLOW,
    "PaymentsToAcquirePropertyPlantAndEquipment": CAPEX,
    "CashAndCashEquivalentsAtCarryingValue": CASH_AND_EQUIVALENTS,
    "ShortTermInvestments": SHORT_TERM_INVESTMENTS,
    "Revenues": REVENUE,
    "RevenueFromContractWithCustomerExcludingAssessedTax": REVENUE,
    "NetIncomeLoss": NET_INCOME,
    "CommonStockSharesOutstanding": SHARES_OUTSTANDING,
}
