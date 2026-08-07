-- Snowsight dashboard tile queries — one statement per tile.
-- Create the dashboard in Snowsight: Projects → Dashboards → + Dashboard → "ContextMesh Cost".
-- For each tile: New tile → From SQL Worksheet, paste the query, run it, then set the chart
-- type/config noted above each query. Context for every tile: CONTEXTMESH_DB.TELEMETRY,
-- warehouse CONTEXTMESH_WH.

----------------------------------------------------------------------------------------
-- TILE 1 · "Total cost — B vs A"  · Scorecard
-- Chart: Scorecard, value = SAVINGS_PCT (or show COST_B / COST_A as two scorecard tiles)
SELECT
  MAX(IFF(ARM = 'A',  TOTAL_COST_USD, NULL))                 AS COST_A_USD,
  MAX(IFF(ARM = 'B' AND PHASE = 'cold', TOTAL_COST_USD, NULL)) AS COST_B_USD,
  ROUND(100 * (COST_A_USD - COST_B_USD) / COST_A_USD, 1)     AS SAVINGS_PCT
FROM V_SUCCESS_COST
WHERE (ARM = 'A' AND PHASE = 'cold') OR (ARM = 'B' AND PHASE = 'cold');

----------------------------------------------------------------------------------------
-- TILE 2 · "Success rate per arm"  · Bar
-- Chart: Bar · X = ARM_PHASE · Y = SUCCESS_RATE  (success must sit next to every cost)
SELECT ARM || ' ' || PHASE AS ARM_PHASE, SUCCESS_RATE, RUNS, SUCCESSES
FROM V_SUCCESS_COST ORDER BY ARM_PHASE;

----------------------------------------------------------------------------------------
-- TILE 3 · "Cost per task by arm"  · Bar (grouped)
-- Chart: Bar · X = TASK_ID · Y = AVG_COST_USD · Group by = ARM_PHASE (grouped, not stacked)
SELECT TASK_ID, ARM || ' ' || PHASE AS ARM_PHASE,
       ROUND(AVG(TOTAL_COST_USD), 4) AS AVG_COST_USD
FROM V_RUN_COST
GROUP BY TASK_ID, ARM_PHASE
ORDER BY TASK_ID, ARM_PHASE;

----------------------------------------------------------------------------------------
-- TILE 4 · "Falling cost as the agent remembers"  · Line   ← the headline graph
-- Chart: Line · X = TASK_SEQ · Y = AVG_COST_USD · Group by = SERIES
-- Arm A flat baseline vs B-cold declining vs B-warm floor.
SELECT TASK_SEQ,
       CASE WHEN ARM = 'A' THEN 'A stock'
            WHEN PHASE = 'cold' THEN 'B cold (store filling)'
            ELSE 'B warm (store full)' END AS SERIES,
       ROUND(AVG(TOTAL_COST_USD), 4) AS AVG_COST_USD
FROM V_RUN_COST
WHERE ARM IN ('A', 'B')
GROUP BY TASK_SEQ, SERIES
ORDER BY TASK_SEQ;

----------------------------------------------------------------------------------------
-- TILE 5 · "Wasted duplicate-read tokens"  · Bar
-- Chart: Bar · X = ARM_PHASE · Y = DUPLICATE_SERVED_TOKENS
SELECT ARM || ' ' || PHASE AS ARM_PHASE,
       DUPLICATE_READS, DUPLICATE_SERVED_TOKENS, TOKENS_SAVED_BY_DIGEST
FROM V_REDUNDANCY ORDER BY ARM_PHASE;

----------------------------------------------------------------------------------------
-- TILE 6 · "Most re-read files (arm A)"  · Bar
-- Chart: Bar (horizontal) · X = DUPLICATE_SERVED_TOKENS · Y = FILE_PATH
SELECT FILE_PATH, READS, DUPLICATE_READS, DUPLICATE_SERVED_TOKENS
FROM V_FILE_HOTLIST
WHERE ARM = 'A'
ORDER BY DUPLICATE_SERVED_TOKENS DESC
LIMIT 10;

----------------------------------------------------------------------------------------
-- TILE 7 · "Who spends the tokens"  · Bar (stacked)
-- Chart: Bar · X = ARM_PHASE · Y = COST_USD · Group by = AGENT_ROLE (stacked)
SELECT ARM || ' ' || PHASE AS ARM_PHASE, AGENT_ROLE,
       ROUND(SUM(COST_USD), 2) AS COST_USD
FROM V_SUBAGENT_ATTRIBUTION
GROUP BY ARM_PHASE, AGENT_ROLE
ORDER BY ARM_PHASE, AGENT_ROLE;

----------------------------------------------------------------------------------------
-- TILE 8 · "Cache economics"  · Bar (stacked)
-- Chart: Bar · X = ARM_PHASE · Y = TOKENS · Group by = TOKEN_CLASS (stacked)
-- A0 vs A isolates what native caching gives before ContextMesh adds anything.
SELECT ARM || ' ' || PHASE AS ARM_PHASE, TOKEN_CLASS, SUM(TOKENS) AS TOKENS
FROM V_RUN_COST
UNPIVOT (TOKENS FOR TOKEN_CLASS IN (INPUT_TOKENS, OUTPUT_TOKENS, CACHE_READ_TOKENS, CACHE_WRITE_TOKENS))
GROUP BY ARM_PHASE, TOKEN_CLASS
ORDER BY ARM_PHASE, TOKEN_CLASS;

----------------------------------------------------------------------------------------
-- TILE 9 · "Two meters agree (proxy vs opencode)"  · Scatter
-- Chart: Scatter · X = NATIVE_TOTAL_TOKENS · Y = PROXY_TOTAL_TOKENS
-- Points hugging the diagonal = the credibility slide.
SELECT NATIVE_TOTAL_TOKENS, PROXY_TOTAL_TOKENS, ARM
FROM V_CROSSCHECK;

----------------------------------------------------------------------------------------
-- TILE 10 · "Honest overhead"  · Scorecard
-- Chart: Scorecard, value = OVERHEAD_USD; secondary = NET_SAVINGS_USD
-- Summarizer + extraction cost is included in B's total (netted), shown here explicitly.
SELECT ROUND(SUM(OVERHEAD_COST_USD), 2) AS OVERHEAD_USD
FROM V_RUN_COST WHERE ARM = 'B';

----------------------------------------------------------------------------------------
-- TILE 11 · "Digest hit & escape-hatch rates"  · Scorecard (x2)
SELECT ROUND(100 * DIGEST_SERVE_RATE, 1) AS DIGEST_SERVE_PCT,
       ROUND(100 * ESCAPE_HATCH_RATE, 1) AS ESCAPE_HATCH_PCT
FROM V_REDUNDANCY
WHERE ARM = 'B' AND PHASE = 'cold';
