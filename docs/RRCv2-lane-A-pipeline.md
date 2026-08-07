# Lane A — Solve loop (ship-it-fast)

Read `RRCv2-plan.md` (goal + frozen contract) first. Least code that turns a `Task` into a tested result and returns token counts. **Backend-agnostic:** you call injected `complete`/`memory`, so you don't care that the model is Codex CLI or that memory is EverOS. Runs offline with a fake `complete` and `NoMemory`.

## Files (yours; nothing collides with Lane B)
```
rrc/pipeline/solve.py     # solve(), render()
rrc/pipeline/stages.py    # spec(), implement(), repair() — prompt strings + parse
rrc/pipeline/verify.py    # run_pytest() (subprocess + timeout)
```
Import only from `rrc.contract`.

## `solve()` — the whole loop
```python
def solve(task, *, warm, complete, memory, strong, cheap) -> Outcome:
    spec_tok = 0
    hit = memory.get(task) if warm else None          # EverOS semantic match (Lane B)
    if hit:
        spec = render(hit, task.params)               # str.format(); 0 spec tokens
    else:
        raw, spec_tok = complete(spec_prompt(task), strong)   # expensive spec ONCE per family
        spec = parse_spec(raw, task)
        memory.put(task, to_template(spec, task.params))      # store for reuse
    code, impl_tok = complete(impl_prompt(spec), cheap)
    passed, out = run_pytest(code, spec.tests)
    rep_tok = 0
    if not passed:                                    # ONE cheap repair, then give up
        code, rep_tok = complete(repair_prompt(spec, code, out), cheap)
        passed, _ = run_pytest(code, spec.tests)
    return Outcome(task.task_id, warm, passed, bool(hit), spec_tok, impl_tok, rep_tok)
```

## Stages — prompts must force SINGLE-SHOT output (Codex is an agent)
Codex will otherwise run commands / edit files / take extra turns = extra tokens + latency. Every prompt ends with: **"Output only <the artifact>. Do not run commands, do not edit files, do not explain."**
- `spec(task)` → returns JSON `{signature, template, tests}` with `{param}` placeholders. `json.loads`; if not valid JSON, one try/except → treat as fail (don't chase it).
- `implement(spec)` → "Output only the Python code. Match the signature; pass the tests."
- `repair(spec, code, pytest_output)` → same as implement + the failing output appended.

## `verify.py`
```python
def run_pytest(code, tests, timeout=15):
    # write code+tests to a temp .py, subprocess pytest, return (returncode==0, output)
```
The timeout is the one guard that matters (a hung run blocks everything). Nothing else.

## `render` / `to_template`
- `to_template(spec, params)`: replace each concrete param value with `{key}`. The workload *gives* you `params`, so this is find-and-replace, not parsing.
- `render(spec, params)`: `spec.signature.format(**params)`, same for template/tests.

## Cut (don't build)
pyright, ruff, PRIME, structural match, sanity check, independent tests, escalation beyond one repair, any memory logic, any Codex/EverOS/Snowflake specifics (all behind ports). Malformed spec JSON → count as fail and move on; non-crashing bugs are fine.

## Self-test (no Lane B, no Codex, no EverOS)
```python
solve(task, warm=False, complete=lambda p,m:(CANNED_CODE,120), memory=NoMemory(), strong="s", cheap="c")
```
Assert: cold runs and returns an Outcome; deliberately-failing canned code triggers exactly one repair; with a real dict `memory`, `reused` is True on the 2nd task whose `get()` returns the stored spec.

## Merge
Lane B injects its real `complete` (Codex) and `memory` (EverOS) into your `solve` unchanged. Nothing in `rrc/pipeline/` should need edits.
