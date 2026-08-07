-- ContextMesh telemetry warehouse — schema, tables, views.
-- Run: snow sql -c contextmesh -f dashboard/snowflake/01_setup.sql
--
-- Data model mirrors docs/IMPLEMENTATION-BRIEF.md §5 and TokenTracker's record schema:
-- one row per LLM call in AGENT_TOKEN_EVENTS; file reads and outcomes in side tables;
-- cost is always computed from the pinned MODEL_RATES table, never hardcoded in charts.

CREATE WAREHOUSE IF NOT EXISTS CONTEXTMESH_WH
  WAREHOUSE_SIZE = XSMALL AUTO_SUSPEND = 60 AUTO_RESUME = TRUE INITIALLY_SUSPENDED = TRUE;

CREATE DATABASE IF NOT EXISTS CONTEXTMESH_DB;
CREATE SCHEMA IF NOT EXISTS CONTEXTMESH_DB.TELEMETRY;
USE SCHEMA CONTEXTMESH_DB.TELEMETRY;
USE WAREHOUSE CONTEXTMESH_WH;

-- Pinned rate table (USD per 1M tokens) — same numbers the proxy will ship with.
CREATE OR REPLACE TABLE MODEL_RATES (
  MODEL                  STRING NOT NULL,
  INPUT_USD_PER_M        FLOAT,
  OUTPUT_USD_PER_M       FLOAT,
  CACHE_WRITE_USD_PER_M  FLOAT,
  CACHE_READ_USD_PER_M   FLOAT
);
INSERT INTO MODEL_RATES VALUES
  ('claude-sonnet-4-5', 3.00, 15.00, 3.75, 0.30),
  ('claude-haiku-4-5',  1.00,  5.00, 1.25, 0.10);

-- One row per proxied LLM request (event_type='llm_call') or summarizer call ('digest_gen').
CREATE OR REPLACE TABLE AGENT_TOKEN_EVENTS (
  RECORD_ID          STRING,
  TS                 TIMESTAMP_NTZ,
  RUN_ID             STRING,
  ARM                STRING,        -- A0 | A | B
  PHASE              STRING,        -- cold | warm
  TASK_ID            STRING,
  TASK_SEQ           INT,           -- position of the task in the suite (x-axis of the falling-cost curve)
  TRIAL              INT,
  EVENT_TYPE         STRING,        -- llm_call | digest_gen
  SESSION_ID         STRING,
  PARENT_SESSION_ID  STRING,
  AGENT_ROLE         STRING,        -- orchestrator | subagent | summarizer
  MODEL              STRING,
  PROVIDER           STRING DEFAULT 'anthropic',
  INPUT_TOKENS       INT,
  OUTPUT_TOKENS      INT,
  CACHE_READ_TOKENS  INT,
  CACHE_WRITE_TOKENS INT,
  STATUS             INT,
  FINISH_REASON      STRING,
  ERROR              STRING,
  SOURCE             STRING DEFAULT 'synthetic'   -- synthetic | proxy
);

-- One row per read-tool invocation observed by the plugin/proxy.
CREATE OR REPLACE TABLE FILE_READ_EVENTS (
  TS                TIMESTAMP_NTZ,
  RUN_ID            STRING,
  ARM               STRING,
  PHASE             STRING,
  TASK_ID           STRING,
  TRIAL             INT,
  SESSION_ID        STRING,
  PARENT_SESSION_ID STRING,
  FILE_PATH         STRING,
  CONTENT_HASH      STRING,
  RAW_TOKENS        INT,          -- what the read would cost un-digested
  SERVED_TOKENS     INT,          -- what actually reached the model
  WAS_DIGEST        BOOLEAN,
  ESCAPE_HATCH      BOOLEAN,      -- ranged re-read after a digest was served
  SOURCE            STRING DEFAULT 'synthetic'
);

-- One row per (arm, phase, task, trial) run; SESSION_ID is the orchestrator session.
CREATE OR REPLACE TABLE TASK_OUTCOMES (
  RUN_ID       STRING,
  ARM          STRING,
  PHASE        STRING,
  TASK_ID      STRING,
  TRIAL        INT,
  SESSION_ID   STRING,
  SUCCESS      BOOLEAN,
  RETRIES      INT,
  COMPLETED_TS TIMESTAMP_NTZ,
  SOURCE       STRING DEFAULT 'synthetic'
);

-- opencode's own per-session accounting (from its sqlite), for the credibility cross-check.
CREATE OR REPLACE TABLE NATIVE_ACCOUNTING (
  SESSION_ID          STRING,
  ARM                 STRING,
  PHASE               STRING,
  TASK_ID             STRING,
  TRIAL               INT,
  PROXY_TOTAL_TOKENS  INT,
  NATIVE_TOTAL_TOKENS INT,
  SOURCE              STRING DEFAULT 'synthetic'
);

----------------------------------------------------------------------------------------
-- Views (names match the implementation brief §5)
----------------------------------------------------------------------------------------

-- Base: every event priced from the pinned rate table.
CREATE OR REPLACE VIEW V_EVENT_COST AS
SELECT e.*,
       (e.INPUT_TOKENS       * r.INPUT_USD_PER_M
      + e.OUTPUT_TOKENS      * r.OUTPUT_USD_PER_M
      + e.CACHE_WRITE_TOKENS * r.CACHE_WRITE_USD_PER_M
      + e.CACHE_READ_TOKENS  * r.CACHE_READ_USD_PER_M) / 1e6 AS COST_USD
FROM AGENT_TOKEN_EVENTS e
JOIN MODEL_RATES r ON e.MODEL = r.MODEL;

-- Per-run rollup: tokens, cost (overhead broken out), success.
CREATE OR REPLACE VIEW V_RUN_COST AS
SELECT e.RUN_ID, e.ARM, e.PHASE, e.TASK_ID, ANY_VALUE(e.TASK_SEQ) AS TASK_SEQ, e.TRIAL,
       SUM(e.INPUT_TOKENS)        AS INPUT_TOKENS,
       SUM(e.OUTPUT_TOKENS)       AS OUTPUT_TOKENS,
       SUM(e.CACHE_READ_TOKENS)   AS CACHE_READ_TOKENS,
       SUM(e.CACHE_WRITE_TOKENS)  AS CACHE_WRITE_TOKENS,
       SUM(e.COST_USD)                                          AS TOTAL_COST_USD,
       SUM(IFF(e.EVENT_TYPE = 'digest_gen', e.COST_USD, 0))     AS OVERHEAD_COST_USD,
       ANY_VALUE(o.SUCCESS) AS SUCCESS,
       ANY_VALUE(o.RETRIES) AS RETRIES
FROM V_EVENT_COST e
LEFT JOIN TASK_OUTCOMES o
  ON  o.RUN_ID = e.RUN_ID AND o.ARM = e.ARM AND o.PHASE = e.PHASE
  AND o.TASK_ID = e.TASK_ID AND o.TRIAL = e.TRIAL
GROUP BY e.RUN_ID, e.ARM, e.PHASE, e.TASK_ID, e.TRIAL;

-- Arm-level summary: total/avg cost, success rate, overhead — the headline numbers.
CREATE OR REPLACE VIEW V_SUCCESS_COST AS
SELECT ARM, PHASE,
       COUNT(*)                             AS RUNS,
       SUM(IFF(SUCCESS, 1, 0))              AS SUCCESSES,
       ROUND(AVG(IFF(SUCCESS, 1, 0)), 3)    AS SUCCESS_RATE,
       ROUND(SUM(TOTAL_COST_USD), 2)        AS TOTAL_COST_USD,
       ROUND(AVG(TOTAL_COST_USD), 4)        AS AVG_COST_PER_RUN_USD,
       ROUND(SUM(OVERHEAD_COST_USD), 2)     AS OVERHEAD_COST_USD,
       SUM(INPUT_TOKENS + OUTPUT_TOKENS + CACHE_READ_TOKENS + CACHE_WRITE_TOKENS) AS TOTAL_TOKENS
FROM V_RUN_COST
GROUP BY ARM, PHASE;

-- Duplicate-read waste per arm — the thesis-justifying view.
CREATE OR REPLACE VIEW V_REDUNDANCY AS
WITH reads AS (
  SELECT f.*,
         ROW_NUMBER() OVER (PARTITION BY ARM, PHASE, TASK_ID, TRIAL, CONTENT_HASH ORDER BY TS) AS RN
  FROM FILE_READ_EVENTS f
)
SELECT ARM, PHASE,
       COUNT(*)                                        AS TOTAL_READS,
       SUM(IFF(RN > 1, 1, 0))                          AS DUPLICATE_READS,
       SUM(SERVED_TOKENS)                              AS SERVED_TOKENS,
       SUM(IFF(RN > 1, SERVED_TOKENS, 0))              AS DUPLICATE_SERVED_TOKENS,
       SUM(IFF(WAS_DIGEST, RAW_TOKENS - SERVED_TOKENS, 0)) AS TOKENS_SAVED_BY_DIGEST,
       ROUND(AVG(IFF(WAS_DIGEST, 1, 0)), 3)            AS DIGEST_SERVE_RATE,
       ROUND(AVG(IFF(WAS_DIGEST, IFF(ESCAPE_HATCH, 1, 0), NULL)), 3) AS ESCAPE_HATCH_RATE
FROM reads
GROUP BY ARM, PHASE;

-- Which files get re-read the most (top-N tile filters on ARM='A').
CREATE OR REPLACE VIEW V_FILE_HOTLIST AS
WITH reads AS (
  SELECT f.*,
         ROW_NUMBER() OVER (PARTITION BY ARM, PHASE, TASK_ID, TRIAL, CONTENT_HASH ORDER BY TS) AS RN
  FROM FILE_READ_EVENTS f
)
SELECT ARM, PHASE, FILE_PATH,
       COUNT(*)                           AS READS,
       SUM(IFF(RN > 1, 1, 0))             AS DUPLICATE_READS,
       SUM(IFF(RN > 1, SERVED_TOKENS, 0)) AS DUPLICATE_SERVED_TOKENS
FROM reads
GROUP BY ARM, PHASE, FILE_PATH;

-- Cost split by who spent it (orchestrator / subagent / summarizer), per session.
CREATE OR REPLACE VIEW V_SUBAGENT_ATTRIBUTION AS
SELECT ARM, PHASE, TASK_ID, TRIAL, AGENT_ROLE, SESSION_ID, PARENT_SESSION_ID,
       COUNT(*)          AS CALLS,
       SUM(COST_USD)     AS COST_USD,
       SUM(INPUT_TOKENS + OUTPUT_TOKENS + CACHE_READ_TOKENS + CACHE_WRITE_TOKENS) AS TOTAL_TOKENS
FROM V_EVENT_COST
GROUP BY ARM, PHASE, TASK_ID, TRIAL, AGENT_ROLE, SESSION_ID, PARENT_SESSION_ID;

-- Proxy vs opencode-native accounting — the "two meters agree" slide.
CREATE OR REPLACE VIEW V_CROSSCHECK AS
SELECT SESSION_ID, ARM, PHASE, TASK_ID, TRIAL,
       PROXY_TOTAL_TOKENS, NATIVE_TOTAL_TOKENS,
       ROUND(100.0 * (PROXY_TOTAL_TOKENS - NATIVE_TOTAL_TOKENS) / NULLIF(NATIVE_TOTAL_TOKENS, 0), 2) AS PCT_DIFF
FROM NATIVE_ACCOUNTING;

SELECT 'setup complete' AS STATUS;
