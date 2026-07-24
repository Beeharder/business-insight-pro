-- Schema for the local DuckDB store.
--
-- One rule governs every table here (PRD §8.3): each row that represents a
-- fact about the world carries an `available_at` timestamp saying when that
-- fact first became knowable to someone sitting at a desk. Nothing is ever
-- updated in place. A correction is a new row with a later `available_at`.
--
-- That single convention is what makes the point-in-time reader in asof.py
-- possible, and it is the difference between a backtest that means something
-- and one that quietly cheats.

-- ---------------------------------------------------------------------------
-- Reference data
-- ---------------------------------------------------------------------------

-- Every security that has ever been in the universe, INCLUDING ones that were
-- later delisted or acquired. PRD §8.3: if this table only holds companies
-- that still exist today, every backtest run against it is wrong, because the
-- losers have been silently deleted from history.
CREATE TABLE IF NOT EXISTS securities (
    ticker              VARCHAR NOT NULL,
    cik                 VARCHAR,              -- SEC identifier; stable across ticker changes
    name                VARCHAR,
    exchange            VARCHAR,
    sector              VARCHAR,
    sector_etf          VARCHAR,              -- benchmark for the §5.1 relative test
    security_type       VARCHAR,              -- common_stock | adr | spac | etf | ...
    ipo_date            DATE,
    first_trade_date    DATE,
    last_trade_date     DATE,                 -- NULL while still trading
    delisted_date       DATE,                 -- NULL while still listed
    delisting_reason    VARCHAR,
    delisting_notice_date DATE,               -- when a notice became public (§4.2)
    available_at        TIMESTAMP NOT NULL,   -- when this row's facts were knowable
    source              VARCHAR,
    PRIMARY KEY (ticker, available_at)
);

-- Daily bars, stored RAW — not back-adjusted.
--
-- Back-adjusted price history is the most common accidental lookahead there
-- is: today's adjustment factors encode splits and dividends that had not
-- happened yet on the date being simulated. We store what actually printed and
-- apply only the corporate actions that were already public (see asof.py).
CREATE TABLE IF NOT EXISTS bars (
    ticker          VARCHAR NOT NULL,
    session_date    DATE NOT NULL,
    open            DOUBLE,
    high            DOUBLE,
    low             DOUBLE,
    close           DOUBLE,
    volume          BIGINT,
    trade_count     BIGINT,
    vwap            DOUBLE,
    is_halted       BOOLEAN DEFAULT FALSE,    -- no trading that session (§8.5)
    available_at    TIMESTAMP NOT NULL,       -- normally session close
    source          VARCHAR,
    PRIMARY KEY (ticker, session_date)
);

-- Splits and cash dividends, indexed by the date they were ANNOUNCED as well
-- as the ex-date. A split announced on the 3rd with an ex-date on the 20th is
-- knowable from the 3rd, but must not be applied to prices before the 20th.
CREATE TABLE IF NOT EXISTS corporate_actions (
    ticker          VARCHAR NOT NULL,
    ex_date         DATE NOT NULL,
    action_type     VARCHAR NOT NULL,         -- split | cash_dividend
    ratio           DOUBLE,                   -- 2.0 for a 2-for-1 split
    cash_amount     DOUBLE,                   -- per share, for dividends
    announced_at    TIMESTAMP,
    available_at    TIMESTAMP NOT NULL,
    source          VARCHAR,
    PRIMARY KEY (ticker, ex_date, action_type)
);

-- Broad market levels used by the regime filter (§7.3): SPY closes and VIX.
CREATE TABLE IF NOT EXISTS index_levels (
    symbol          VARCHAR NOT NULL,
    session_date    DATE NOT NULL,
    close           DOUBLE,
    available_at    TIMESTAMP NOT NULL,
    source          VARCHAR,
    PRIMARY KEY (symbol, session_date)
);

-- ---------------------------------------------------------------------------
-- Filings and fundamentals
-- ---------------------------------------------------------------------------

-- Indexed by FILING DATE, not period end (PRD §8.3). A fiscal quarter ending
-- 31 March is not knowable on 31 March; it is knowable when the 10-Q is filed,
-- typically five to six weeks later.
CREATE TABLE IF NOT EXISTS filings (
    accession           VARCHAR NOT NULL PRIMARY KEY,
    cik                 VARCHAR,
    ticker              VARCHAR NOT NULL,
    form_type           VARCHAR NOT NULL,     -- 10-K | 10-Q | 8-K | ...
    period_end          DATE,
    filing_date         DATE NOT NULL,
    accepted_at         TIMESTAMP,
    available_at        TIMESTAMP NOT NULL,
    primary_doc_url     VARCHAR,
    -- Deterministic flags read out of the filing text, used by the §4.2
    -- survivability gate before any LLM is involved.
    going_concern_flag      BOOLEAN DEFAULT FALSE,
    delisting_notice_flag   BOOLEAN DEFAULT FALSE,
    default_or_covenant_flag BOOLEAN DEFAULT FALSE,
    -- Pointers to text on disk rather than blobs in the database, so the .duckdb
    -- file stays small enough to open and copy easily.
    item_1a_ref         VARCHAR,              -- Risk Factors, for Strategy B1
    raw_ref             VARCHAR,
    source              VARCHAR
);

-- Reported financial values, one row per (period, metric, filing).
--
-- A restatement never overwrites the original: it lands as a NEW row carrying
-- the amending filing's date. That is what lets asof.py answer both "what did
-- the company originally report" and "what would I have seen on this date".
CREATE TABLE IF NOT EXISTS fundamentals (
    cik             VARCHAR,
    ticker          VARCHAR NOT NULL,
    period_end      DATE NOT NULL,
    fiscal_period   VARCHAR,                  -- FY | Q1 | Q2 | Q3 | Q4
    metric          VARCHAR NOT NULL,         -- see store/metrics.py for the vocabulary
    value           DOUBLE,
    unit            VARCHAR,
    filing_date     DATE NOT NULL,
    accession       VARCHAR,
    is_restatement  BOOLEAN DEFAULT FALSE,
    available_at    TIMESTAMP NOT NULL,
    source          VARCHAR,
    PRIMARY KEY (ticker, period_end, metric, filing_date)
);

-- ---------------------------------------------------------------------------
-- Text sources
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS news_articles (
    article_id      VARCHAR NOT NULL PRIMARY KEY,
    ticker          VARCHAR NOT NULL,
    published_at    TIMESTAMP NOT NULL,
    source_name     VARCHAR,
    headline        VARCHAR,
    url             VARCHAR,
    body_ref        VARCHAR,
    available_at    TIMESTAMP NOT NULL,       -- = published_at for live feeds
    ingested_at     TIMESTAMP,
    source          VARCHAR
);

CREATE TABLE IF NOT EXISTS transcripts (
    transcript_id   VARCHAR NOT NULL PRIMARY KEY,
    ticker          VARCHAR NOT NULL,
    event_date      DATE NOT NULL,
    fiscal_period   VARCHAR,
    call_type       VARCHAR,                  -- earnings | guidance | investor_day
    text_ref        VARCHAR,
    available_at    TIMESTAMP NOT NULL,
    source          VARCHAR
);

-- Scheduled earnings dates, for the §7.2 three-day blackout. `announced_at`
-- matters: a date confirmed on the 10th is not knowable on the 5th.
CREATE TABLE IF NOT EXISTS earnings_calendar (
    ticker          VARCHAR NOT NULL,
    scheduled_date  DATE NOT NULL,
    is_confirmed    BOOLEAN DEFAULT FALSE,
    announced_at    TIMESTAMP,
    available_at    TIMESTAMP NOT NULL,
    source          VARCHAR,
    PRIMARY KEY (ticker, scheduled_date, available_at)
);

-- ---------------------------------------------------------------------------
-- Operational record (PRD §8.4) — every decision reconstructible after the fact
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS runs (
    run_id          VARCHAR NOT NULL PRIMARY KEY,
    mode            VARCHAR,                  -- backtest | paper | live
    started_at      TIMESTAMP NOT NULL,
    finished_at     TIMESTAMP,
    status          VARCHAR,                  -- running | ok | failed
    as_of_date      DATE,
    error           VARCHAR,
    stages_json     VARCHAR
);

-- One row per screen decision per ticker. This is the table you read when you
-- want to know why the system did or did not do something on a given day.
CREATE TABLE IF NOT EXISTS decision_log (
    log_id          BIGINT NOT NULL,
    run_id          VARCHAR NOT NULL,
    ts              TIMESTAMP NOT NULL,
    as_of_date      DATE NOT NULL,
    ticker          VARCHAR,
    strategy        VARCHAR,                  -- A | B | shared
    stage           VARCHAR NOT NULL,         -- universe | survivability | shock | decay | llm | limits | order
    screen_name     VARCHAR,
    passed          BOOLEAN,
    verdict         VARCHAR,
    reason          VARCHAR,
    detail_json     VARCHAR,
    PRIMARY KEY (log_id)
);

CREATE SEQUENCE IF NOT EXISTS decision_log_seq START 1;

-- Raw prompt and raw response, kept verbatim (PRD §8.4). Without the exact
-- prompt text you cannot tell a model problem from a prompt problem.
CREATE TABLE IF NOT EXISTS llm_calls (
    call_id         VARCHAR NOT NULL PRIMARY KEY,
    run_id          VARCHAR NOT NULL,
    ts              TIMESTAMP NOT NULL,
    ticker          VARCHAR,
    strategy        VARCHAR,
    model           VARCHAR,
    prompt          VARCHAR,
    response        VARCHAR,
    schema_valid    BOOLEAN,
    validation_error VARCHAR,
    input_tokens    BIGINT,
    output_tokens   BIGINT,
    latency_ms      BIGINT
);

CREATE TABLE IF NOT EXISTS orders (
    order_id        VARCHAR NOT NULL PRIMARY KEY,
    run_id          VARCHAR NOT NULL,
    ts              TIMESTAMP NOT NULL,
    ticker          VARCHAR NOT NULL,
    strategy        VARCHAR,                  -- which strategy gets the credit (§7.1)
    side            VARCHAR,                  -- buy | sell
    qty             DOUBLE,
    order_type      VARCHAR,
    intended_at     DATE,
    status          VARCHAR,                  -- queued | filled | partial | rejected | cancelled
    fill_qty        DOUBLE,
    fill_price      DOUBLE,
    broker_order_id VARCHAR,
    reason          VARCHAR
);

-- How fresh each source is. The §7.2 "refuses to trade on stale data" check
-- reads this, and the digest's system-health section reports it.
CREATE TABLE IF NOT EXISTS ingest_watermarks (
    source          VARCHAR NOT NULL,
    entity          VARCHAR NOT NULL,
    last_success_at TIMESTAMP,
    last_attempt_at TIMESTAMP,
    covered_through DATE,
    rows_written    BIGINT,
    last_error      VARCHAR,
    PRIMARY KEY (source, entity)
);
