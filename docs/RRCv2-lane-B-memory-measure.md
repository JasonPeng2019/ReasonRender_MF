# Lane B — Codex CLI, EverOS, Snowflake, Harness (ship-it-fast)

Read `RRCv2-plan.md` first. You own the three required backends + the harness + the curve. Build in requirement order: **Codex `complete` → EverOS `Memory` → run/curve (Milestone 1)**, then **Snowflake (Milestone 2)**. Test against a stub `solve` until Lane A lands.

## Files (yours; nothing collides with Lane A)
```
rrc/model.py       # complete(): shells to `codex exec --json`, returns (text, tokens)
rrc/memory.py      # EverOSMemory (implements Memory); LocalMemory (dev fallback only)
rrc/everos.py      # everos_add, everos_flush, everos_search (thin HTTP)
rrc/workload.py    # gen_workload(): repeating families with params + text + oracle tests
rrc/run.py         # run_arm(), curve/print, main()
rrc/sink.py        # Snowflake logging + curve SQL (Milestone 2)
```
Import only from `rrc.contract` (plus Lane A's `solve` in `run.py`, at the end).

## 1) `complete()` — Codex CLI, subscription, headless
```python
import subprocess, json
def complete(prompt, model):
    cmd = ["codex","exec","--json","--skip-git-repo-check","--sandbox","read-only"]
    if model: cmd += ["--model", model]
    cmd += [prompt]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=180).stdout
    text, tokens = "", 0
    for line in out.splitlines():
        try: ev = json.loads(line)
        except: continue
        # keep the last agent/assistant message text
        if ev.get("type","").endswith("message") and ev.get("text"): text = ev["text"]
        # turn.completed carries token usage
        if ev.get("type") == "turn.completed":
            u = ev.get("usage", {})
            tokens = u.get("total_tokens") or (u.get("input_tokens",0)+u.get("output_tokens",0))
    return text, tokens
```
- Auth: run `codex login` once (ChatGPT subscription) — no API key.
- **Confirm the exact event names/fields with a real run** (`codex exec --json ... "say hi" | tail`); versions differ. If token parsing is flaky, fall back to `-o msg.txt` for the text + tiktoken over prompt+text (consistent across arms).
- Two models: pass `strong`/`cheap` as `--model` (or reasoning-effort profiles). If the subscription exposes only one, use it for both.

## 2) `EverOSMemory` — the required memory, no fork patch
```python
class EverOSMemory:
    def __init__(self): self.d = {}                       # family -> Spec (the real artifact)
    def put(self, task, spec):
        self.d[task.family] = spec
        everos_add(session_id=task.family, user_id="rrc", text=task.text)  # POST /add
        everos_flush(session_id=task.family)                                # POST /flush (sync md)
    def get(self, task):
        hits = everos_search(user_id="rrc", query=task.text, top_k=1, min_score=TAU)  # POST /search
        if hits and hits[0]["session_id"] in self.d:      # session_id == family (the join)
            return self.d[hits[0]["session_id"]]
        return None
```
- `everos_search` calls `/api/v2/memory/search` (method hybrid default) and returns `data.episodes` (each has `session_id`, `score`). MISS floor = `min_score=TAU` (~0.3–0.4; tune).
- **Index lag:** a just-stored family isn't instantly searchable. Between `put` and a later dependent `get`, poll `GET /health` `cascade.pending`==0 (twice), or interleave families so recurrences aren't adjacent.
- `LocalMemory` (dict keyed by `task.family`) is a dev fallback if EverOS is down — but Milestone 1 ships on EverOS.

## 3) `gen_workload()` — do-or-die: it MUST repeat
```python
def gen_workload():
    # 2-3 families x several entities, INTERLEAVED so families recur.
    # family="crud" over Order/User/Item...; family="parse" over csv/json...
    # each Task: params (entity, fields), text (NL desc -> EverOS match), oracle_tests (scoring only)
    return tasks
```
Flat warm curve ⇒ fix here (or lower TAU), not the pipeline.

## 4) `run.py` — Milestone 1 money shot
```python
def run_arm(tasks, warm, mem_factory):
    mem = mem_factory()
    return [solve(t, warm=warm, complete=complete, memory=mem, strong=STRONG, cheap=CHEAP)
            for t in tasks]

def main():
    tasks = gen_workload()
    cold = run_arm(tasks, warm=False, mem_factory=NoMemory)
    warm = run_arm(tasks, warm=True,  mem_factory=EverOSMemory)
    report(cold, warm)         # running mean tokens/passed vs task order; matplotlib or CSV+print
    log_to_snowflake(cold+warm)  # Milestone 2
```
Count every token (spec+impl+repair). A solved task uses `oracle_passed` when an oracle is present,
otherwise `passed`. A hit uses the same solved predicate plus `reused`.

## 5) `sink.py` — Snowflake cost-of-record (Milestone 2)
```python
# CREATE TABLE rrc_runs(seq int autoincrement, task_id string, arm string, reused boolean,
#   spec_tokens int, impl_tokens int, repair_tokens int, total_tokens int, passed boolean,
#   oracle_passed boolean, ts timestamp);
def log_to_snowflake(outcomes):
    # INSERT one row per outcome (snowflake-connector-python; executemany)
    ...
# Curve in SQL (the demo query):
# SELECT arm, seq,
#        AVG(total_tokens) OVER (PARTITION BY arm ORDER BY seq
#              ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running_mean_tokens
# FROM rrc_runs WHERE COALESCE(oracle_passed, passed) ORDER BY arm, seq;
```
De-scope: if SQL curve is tight on time, just `INSERT` (Snowflake requirement met) and plot in Python.

## Cut (don't build)
Codex query-tags/account-usage reconciliation (Codex `turn.completed` is the meter); EverOS fork patch / external_ref / OME trigger; PRIME; baseline & cascade arms; plotting polish; workload realism beyond "it repeats."

## Self-test (no Lane A)
Stub `solve = lambda **k: Outcome(k["task"].task_id, k["warm"], True, seen_before, 0 if seen_before else 800, 200, 0)`. Verify `run_arm` sums tokens, cold mean > warm mean, warm falls over task order. Separately: `complete("say only: PING","")` returns non-empty text + tokens>0; `EverOSMemory` round-trips put→(wait)→get against a running EverOS.

## Merge
Swap the stub `solve` for Lane A's real `solve` in `run.py`. Everything else unchanged.
