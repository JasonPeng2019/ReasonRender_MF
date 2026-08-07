-- ContextMesh token-economy schema for Snowflake.
-- Source of truth: Tollgate's durable JSONL (one row per LLM request, provider-reported usage)
-- plus the bench harness run summaries (opencode's own accounting + plugin events).

CREATE SCHEMA IF NOT EXISTS CONTEXTMESH;
USE SCHEMA CONTEXTMESH;

-- One row per LLM HTTP request that crossed the Tollgate proxy.
CREATE TABLE IF NOT EXISTS AGENT_TOKEN_EVENTS (
    RECORD_ID           VARCHAR NOT NULL,
    TS                  TIMESTAMP_TZ,
    COMPLETED_TS        TIMESTAMP_TZ,
    SESSION             VARCHAR,          -- '<runid>-<arm>-<mode>' or '...-summarizer' or 'everos-extraction'
    RUN_ID              VARCHAR,          -- parsed from SESSION
    ARM                 VARCHAR,          -- 'a' | 'b'
    MODE                VARCHAR,          -- 'cold' | 'warm'
    IS_SUMMARIZER       BOOLEAN,         -- optimizer overhead (netted IN, reported separately)
    PROVIDER            VARCHAR,
    KIND                VARCHAR,          -- adapter kind: openai | anthropic | gemini
    MODEL               VARCHAR,
    ENDPOINT            VARCHAR,
    STREAM              BOOLEAN,
    STATUS              NUMBER,
    MEASUREMENT_STATE   VARCHAR,          -- exact | partial | missing | estimated
    INPUT_TOKENS        NUMBER,
    OUTPUT_TOKENS       NUMBER,
    REASONING_TOKENS    NUMBER,
    CACHE_READ_TOKENS   NUMBER,
    CACHE_WRITE_TOKENS  NUMBER,
    TOTAL_TOKENS        NUMBER,
    LATENCY_MS          NUMBER,
    REQUEST_ID          VARCHAR,
    RAW                 VARIANT,          -- the full Tollgate record for audit
    CONSTRAINT PK_AGENT_TOKEN_EVENTS PRIMARY KEY (RECORD_ID)
);

-- One row per bench run: opencode sqlite rollup + plugin optimization events.
CREATE TABLE IF NOT EXISTS BENCH_RUN_SUMMARY (
    RUN_ID          VARCHAR,
    LABEL           VARCHAR,             -- 'a-cold' | 'b-cold' | 'b-warm'
    SESSION         VARCHAR,
    EXIT_CODE       NUMBER,
    WALL_SECONDS    FLOAT,
    SUBAGENT_COUNT  NUMBER,
    FULL_READS      NUMBER,
    DUP_FULL_READS  NUMBER,
    OC_INPUT        NUMBER,              -- opencode's own meter (cross-check)
    OC_OUTPUT       NUMBER,
    DIGEST_HITS     NUMBER,
    DIGEST_STORED   NUMBER,
    TASKS_COMPRESSED NUMBER,
    ESCAPE_HATCHES  NUMBER,
    RAW             VARIANT,
    CONSTRAINT PK_BENCH_RUN_SUMMARY PRIMARY KEY (RUN_ID, LABEL)
);

-- Per-run proxy totals (exact provider-reported usage only).
CREATE OR REPLACE VIEW V_RUN_COST AS
SELECT
    RUN_ID,
    ARM,
    MODE,
    SUM(IFF(IS_SUMMARIZER, 0, INPUT_TOKENS))   AS AGENT_INPUT_TOKENS,
    SUM(IFF(IS_SUMMARIZER, 0, OUTPUT_TOKENS))  AS AGENT_OUTPUT_TOKENS,
    SUM(IFF(IS_SUMMARIZER, INPUT_TOKENS, 0))   AS SUMMARIZER_INPUT_TOKENS,
    SUM(IFF(IS_SUMMARIZER, OUTPUT_TOKENS, 0))  AS SUMMARIZER_OUTPUT_TOKENS,
    SUM(INPUT_TOKENS)                          AS TOTAL_INPUT_TOKENS,
    SUM(OUTPUT_TOKENS)                         AS TOTAL_OUTPUT_TOKENS,
    SUM(INPUT_TOKENS + OUTPUT_TOKENS)          AS TOTAL_TOKENS,
    COUNT(*)                                   AS REQUESTS,
    COUNT_IF(MEASUREMENT_STATE = 'exact')      AS EXACT_REQUESTS,
    -- Modeled at reference rates ($3/M in, $15/M out) — tokens are the measurement.
    ROUND(SUM(INPUT_TOKENS) * 3.0 / 1e6 + SUM(OUTPUT_TOKENS) * 15.0 / 1e6, 4) AS MODELED_COST_USD
FROM AGENT_TOKEN_EVENTS
WHERE RUN_ID IS NOT NULL AND MEASUREMENT_STATE = 'exact'
GROUP BY RUN_ID, ARM, MODE;

-- The headline: arm B (cold and warm) vs arm A, per run id. Summarizer INCLUDED in B.
CREATE OR REPLACE VIEW V_ARM_COMPARISON AS
WITH base AS (SELECT * FROM V_RUN_COST)
SELECT
    b.RUN_ID,
    b.MODE                                        AS B_MODE,
    a.TOTAL_TOKENS                                AS A_TOTAL_TOKENS,
    b.TOTAL_TOKENS                                AS B_TOTAL_TOKENS,
    a.TOTAL_INPUT_TOKENS                          AS A_INPUT_TOKENS,
    b.TOTAL_INPUT_TOKENS                          AS B_INPUT_TOKENS,
    ROUND(100 * (a.TOTAL_INPUT_TOKENS - b.TOTAL_INPUT_TOKENS) / NULLIF(a.TOTAL_INPUT_TOKENS, 0), 1) AS INPUT_REDUCTION_PCT,
    ROUND(100 * (a.TOTAL_TOKENS - b.TOTAL_TOKENS) / NULLIF(a.TOTAL_TOKENS, 0), 1)                   AS TOTAL_REDUCTION_PCT,
    a.MODELED_COST_USD                            AS A_MODELED_COST_USD,
    b.MODELED_COST_USD                            AS B_MODELED_COST_USD
FROM base b
JOIN base a ON a.RUN_ID = b.RUN_ID AND a.ARM = 'a' AND a.MODE = 'cold'
WHERE b.ARM = 'b';

-- Redundancy + optimization behavior per run (from the harness summaries).
CREATE OR REPLACE VIEW V_OPTIMIZATION_BEHAVIOR AS
SELECT
    RUN_ID, LABEL, SUBAGENT_COUNT, FULL_READS, DUP_FULL_READS,
    DIGEST_HITS, DIGEST_STORED, TASKS_COMPRESSED, ESCAPE_HATCHES,
    OC_INPUT, OC_OUTPUT, WALL_SECONDS, EXIT_CODE
FROM BENCH_RUN_SUMMARY;

-- Cross-check view: proxy meter vs opencode's own meter per run.
CREATE OR REPLACE VIEW V_METER_CROSSCHECK AS
SELECT
    c.RUN_ID,
    c.ARM || '-' || c.MODE                       AS LABEL,
    c.AGENT_INPUT_TOKENS + c.AGENT_OUTPUT_TOKENS AS PROXY_AGENT_TOKENS,
    s.OC_INPUT + s.OC_OUTPUT                     AS OPENCODE_TOKENS,
    (c.AGENT_INPUT_TOKENS + c.AGENT_OUTPUT_TOKENS) - (s.OC_INPUT + s.OC_OUTPUT) AS DELTA_TOKENS
FROM V_RUN_COST c
JOIN BENCH_RUN_SUMMARY s ON s.RUN_ID = c.RUN_ID AND s.LABEL = c.ARM || '-' || c.MODE;
