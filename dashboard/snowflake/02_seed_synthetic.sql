-- Synthetic seed data (SOURCE='synthetic') so the dashboard is buildable before real
-- proxy runs exist. Encodes the expected story from docs/IMPLEMENTATION-BRIEF.md:
--   A0 = stock, caching stripped (most expensive)
--   A  = stock, native Anthropic caching (headline baseline)
--   B  = + contextmesh plugin, cold suite (digests fill) then warm suite (digests hit)
-- SCALE: deliberately small — 1 orchestrator + 2 subagents per run — to keep credit
-- usage minimal; grow after real integration.
-- Structure (sessions per run, calls per session, files read) is arm-independent so
-- arms stay comparable; only token magnitudes differ by arm.
-- Replace later with real data: DELETE ... WHERE SOURCE='synthetic', then COPY INTO.
-- Run: snow sql -c contextmesh -f dashboard/snowflake/02_seed_synthetic.sql

USE SCHEMA CONTEXTMESH_DB.TELEMETRY;
USE WAREHOUSE CONTEXTMESH_WH;

DELETE FROM AGENT_TOKEN_EVENTS WHERE SOURCE = 'synthetic';
DELETE FROM FILE_READ_EVENTS   WHERE SOURCE = 'synthetic';
DELETE FROM TASK_OUTCOMES      WHERE SOURCE = 'synthetic';
DELETE FROM NATIVE_ACCOUNTING  WHERE SOURCE = 'synthetic';

-- ---------------------------------------------------------------- dimension scaffolding
CREATE OR REPLACE TEMP TABLE _RUNS AS
WITH arms AS (
  SELECT * FROM VALUES ('A0','cold'), ('A','cold'), ('B','cold'), ('B','warm') v(ARM, PHASE)
),
tasks AS (
  SELECT SEQ4() + 1 AS TASK_SEQ, 'task-0' || (SEQ4() + 1) AS TASK_ID
  FROM TABLE(GENERATOR(ROWCOUNT => 8))
),
trials AS (SELECT SEQ4() + 1 AS TRIAL FROM TABLE(GENERATOR(ROWCOUNT => 3)))
SELECT 'run-2026-08-07' AS RUN_ID, a.ARM, a.PHASE, t.TASK_ID, t.TASK_SEQ, tr.TRIAL,
       ABS(HASH(a.ARM || a.PHASE || t.TASK_ID || tr.TRIAL)) AS RSEED
FROM arms a CROSS JOIN tasks t CROSS JOIN trials tr;

-- one orchestrator (IDX 0) + 2 subagents per run
CREATE OR REPLACE TEMP TABLE _SESSIONS AS
WITH idx AS (SELECT SEQ4() AS IDX FROM TABLE(GENERATOR(ROWCOUNT => 3)))
SELECT r.*, i.IDX,
       IFF(i.IDX = 0, 'orchestrator', 'subagent') AS AGENT_ROLE,
       'ses_' || SUBSTR(MD5(r.ARM || r.PHASE || r.TASK_ID || r.TRIAL || i.IDX), 1, 12) AS SESSION_ID,
       IFF(i.IDX = 0, '',
           'ses_' || SUBSTR(MD5(r.ARM || r.PHASE || r.TASK_ID || r.TRIAL || '0'), 1, 12)) AS PARENT_SESSION_ID
FROM _RUNS r CROSS JOIN idx i;

-- ---------------------------------------------------------------- llm_call events
-- Token profile per call:
--   A0: everything is plain input, no cache.
--   A : small fresh input, big cache_read (replayed prefix), cache_write for new content.
--   B : same shape as A but new content is digested, so cache traffic shrinks;
--       cold declines along TASK_SEQ as the digest store fills; warm is cheapest.
INSERT INTO AGENT_TOKEN_EVENTS
  (RECORD_ID, TS, RUN_ID, ARM, PHASE, TASK_ID, TASK_SEQ, TRIAL, EVENT_TYPE,
   SESSION_ID, PARENT_SESSION_ID, AGENT_ROLE, MODEL, PROVIDER,
   INPUT_TOKENS, OUTPUT_TOKENS, CACHE_READ_TOKENS, CACHE_WRITE_TOKENS,
   STATUS, FINISH_REASON, ERROR, SOURCE)
WITH calls AS (
  SELECT s.*, c.CALL_IDX,
         ABS(HASH(s.SESSION_ID || c.CALL_IDX)) AS H,
         CASE WHEN s.ARM = 'B' AND s.PHASE = 'cold' THEN 0.95 - s.TASK_SEQ * 0.05
              WHEN s.ARM = 'B' AND s.PHASE = 'warm' THEN 0.55
              ELSE 1.0 END AS BF
  FROM _SESSIONS s
  CROSS JOIN (SELECT SEQ4() + 1 AS CALL_IDX FROM TABLE(GENERATOR(ROWCOUNT => 8))) c
  WHERE c.CALL_IDX <= 4 + MOD(ABS(HASH(s.SESSION_ID)), 3)     -- 4-6 calls per session
        + IFF(s.AGENT_ROLE = 'orchestrator'
              AND MOD(ABS(HASH(s.RUN_ID || s.ARM || s.PHASE || s.TASK_ID || s.TRIAL || 'ok')), 100)
                  >= CASE WHEN s.ARM = 'A0' THEN 83 WHEN s.ARM = 'A' THEN 92
                          WHEN s.PHASE = 'warm' THEN 94 ELSE 88 END,
              2, 0)                                            -- failed runs: 2 extra repair calls
)
SELECT
  UUID_STRING(),
  DATEADD('second', MOD(H, 36000), '2026-08-07 14:00:00'::TIMESTAMP_NTZ),
  RUN_ID, ARM, PHASE, TASK_ID, TASK_SEQ, TRIAL, 'llm_call',
  SESSION_ID, PARENT_SESSION_ID, AGENT_ROLE, 'claude-sonnet-4-5', 'anthropic',
  CASE WHEN ARM = 'A0' THEN 6000 + MOD(H, 9000)                             -- plain input
       ELSE 20 + MOD(H, 60) END                                AS INPUT_TOKENS,
  IFF(AGENT_ROLE = 'orchestrator', 450 + MOD(H, 700), 300 + MOD(H, 600))    AS OUTPUT_TOKENS,
  CASE WHEN ARM = 'A0' THEN 0
       ELSE ROUND((5000 + MOD(H, 16000) + CALL_IDX * 1200) * BF) END        AS CACHE_READ,
  CASE WHEN ARM = 'A0' THEN 0
       ELSE ROUND((1800 + MOD(H, 2600)) * BF) END                           AS CACHE_WRITE,
  200, 'end_turn', NULL, 'synthetic'
FROM calls;

-- summarizer (digest_gen) calls — B cold only, mostly early tasks, first trial
-- (content-hash keyed digests persist across trials, so only trial 1 pays)
INSERT INTO AGENT_TOKEN_EVENTS
  (RECORD_ID, TS, RUN_ID, ARM, PHASE, TASK_ID, TASK_SEQ, TRIAL, EVENT_TYPE,
   SESSION_ID, PARENT_SESSION_ID, AGENT_ROLE, MODEL, PROVIDER,
   INPUT_TOKENS, OUTPUT_TOKENS, CACHE_READ_TOKENS, CACHE_WRITE_TOKENS,
   STATUS, FINISH_REASON, ERROR, SOURCE)
WITH gen AS (SELECT SEQ4() AS DIDX FROM TABLE(GENERATOR(ROWCOUNT => 8))),
d AS (
  SELECT r.*, g.DIDX, ABS(HASH(r.TASK_ID || 'dg' || g.DIDX)) AS H
  FROM _RUNS r CROSS JOIN gen g
  WHERE r.ARM = 'B' AND r.PHASE = 'cold' AND r.TRIAL = 1
    AND g.DIDX < CASE r.TASK_SEQ WHEN 1 THEN 7 WHEN 2 THEN 4 WHEN 3 THEN 3 ELSE 2 END
)
SELECT
  UUID_STRING(),
  DATEADD('second', MOD(H, 36000), '2026-08-07 14:00:00'::TIMESTAMP_NTZ),
  RUN_ID, ARM, PHASE, TASK_ID, TASK_SEQ, TRIAL, 'digest_gen',
  'contextmesh-summarizer', '', 'summarizer', 'claude-haiku-4-5', 'anthropic',
  1500 + MOD(H, 4300),                  -- raw file in
  ROUND((1500 + MOD(H, 4300)) * 0.28),  -- digest out
  0, 0, 200, 'end_turn', NULL, 'synthetic'
FROM d;

-- ---------------------------------------------------------------- file_read events
-- Each subagent reads 3 core files (pool of 5 shared ones => heavy sibling overlap)
-- plus 1 task-specific file. In B, any read whose content hash was already digested is
-- served as a digest (~30% of raw); ~7% of digest serves trigger a ranged escape-hatch read.
INSERT INTO FILE_READ_EVENTS
WITH reads AS (
  SELECT s.*, ri.READ_IDX,
         ABS(HASH(s.SESSION_ID || 'read' || ri.READ_IDX)) AS H,
         IFF(ri.READ_IDX <= 3,
             DECODE(MOD(ABS(HASH(s.SESSION_ID || 'f' || ri.READ_IDX)), 5),
                    0, 'src/models.py', 1, 'src/db.py', 2, 'src/utils.py',
                    3, 'src/middleware/auth.py', 4, 'src/schemas.py'),
             'src/routes/' || s.TASK_ID || '.py') AS FILE_PATH
  FROM _SESSIONS s
  CROSS JOIN (SELECT SEQ4() + 1 AS READ_IDX FROM TABLE(GENERATOR(ROWCOUNT => 4))) ri
  WHERE s.AGENT_ROLE = 'subagent'
),
sized AS (
  SELECT r.RUN_ID, r.ARM, r.PHASE, r.TASK_ID, r.TRIAL, r.SESSION_ID, r.PARENT_SESSION_ID,
         r.FILE_PATH, r.H,
         DATEADD('second', MOD(r.H, 36000), '2026-08-07 14:00:00'::TIMESTAMP_NTZ) AS TS,
         DECODE(r.FILE_PATH,
                'src/models.py', 5800, 'src/db.py', 3400, 'src/utils.py', 2900,
                'src/middleware/auth.py', 4100, 'src/schemas.py', 5200,
                1200 + MOD(ABS(HASH(r.TASK_ID)), 1800)) AS RAW_TOKENS,
         SUBSTR(MD5(r.FILE_PATH), 1, 16) AS CONTENT_HASH
  FROM reads r
),
flagged AS (
  SELECT s.*,
         ROW_NUMBER() OVER (PARTITION BY ARM, PHASE, CONTENT_HASH ORDER BY TS) AS RN_GLOBAL
  FROM sized s
)
SELECT TS, RUN_ID, ARM, PHASE, TASK_ID, TRIAL, SESSION_ID, PARENT_SESSION_ID,
       FILE_PATH, CONTENT_HASH, RAW_TOKENS,
       CASE WHEN ARM = 'B' AND (PHASE = 'warm' OR RN_GLOBAL > 1)
            THEN ROUND(RAW_TOKENS * 0.30) + IFF(MOD(H, 100) < 7, ROUND(RAW_TOKENS * 0.25), 0)
            ELSE RAW_TOKENS END                                          AS SERVED_TOKENS,
       (ARM = 'B' AND (PHASE = 'warm' OR RN_GLOBAL > 1))                 AS WAS_DIGEST,
       (ARM = 'B' AND (PHASE = 'warm' OR RN_GLOBAL > 1) AND MOD(H, 100) < 7) AS ESCAPE_HATCH,
       'synthetic'
FROM flagged;

-- ---------------------------------------------------------------- outcomes
INSERT INTO TASK_OUTCOMES
WITH o AS (
  SELECT r.*,
         MOD(ABS(HASH(r.RUN_ID || r.ARM || r.PHASE || r.TASK_ID || r.TRIAL || 'ok')), 100)
           < CASE WHEN r.ARM = 'A0' THEN 83 WHEN r.ARM = 'A' THEN 92
                  WHEN r.PHASE = 'warm' THEN 94 ELSE 88 END AS SUCCESS
  FROM _RUNS r
)
SELECT RUN_ID, ARM, PHASE, TASK_ID, TRIAL,
       'ses_' || SUBSTR(MD5(ARM || PHASE || TASK_ID || TRIAL || '0'), 1, 12),
       SUCCESS,
       IFF(SUCCESS, 0, 1 + MOD(RSEED, 2)) AS RETRIES,
       DATEADD('second', MOD(RSEED, 36000) + 600, '2026-08-07 14:00:00'::TIMESTAMP_NTZ),
       'synthetic'
FROM o;

-- ---------------------------------------------------------------- native cross-check
-- opencode's own accounting = proxy totals within ±1.5% measurement noise
INSERT INTO NATIVE_ACCOUNTING
SELECT SESSION_ID, ARM, PHASE, TASK_ID, TRIAL,
       SUM(INPUT_TOKENS + OUTPUT_TOKENS + CACHE_READ_TOKENS + CACHE_WRITE_TOKENS) AS PROXY_TOTAL,
       ROUND(SUM(INPUT_TOKENS + OUTPUT_TOKENS + CACHE_READ_TOKENS + CACHE_WRITE_TOKENS)
             * (0.985 + MOD(ABS(HASH(SESSION_ID)), 200) / 10000.0))               AS NATIVE_TOTAL,
       'synthetic'
FROM AGENT_TOKEN_EVENTS
WHERE EVENT_TYPE = 'llm_call' AND SOURCE = 'synthetic'
GROUP BY SESSION_ID, ARM, PHASE, TASK_ID, TRIAL;

SELECT 'seeded' AS STATUS,
       (SELECT COUNT(*) FROM AGENT_TOKEN_EVENTS) AS TOKEN_EVENTS,
       (SELECT COUNT(*) FROM FILE_READ_EVENTS)   AS FILE_READS,
       (SELECT COUNT(*) FROM TASK_OUTCOMES)      AS OUTCOMES,
       (SELECT COUNT(*) FROM NATIVE_ACCOUNTING)  AS NATIVE_ROWS;
