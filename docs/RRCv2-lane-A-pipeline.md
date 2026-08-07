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
        spec = render(hit, task.params)               # named replacement; 0 spec tokens
    else:
        raw, spec_tok = complete(spec_prompt(task), strong)   # expensive spec ONCE per family
        spec = parse_spec(raw, task)
        if spec is malformed: return failed Outcome   # record spec_tok; no implementation
        spec = render(spec, task.params)
    code, impl_tok = complete(impl_prompt(spec), cheap)
    passed, out = run_pytest(code, spec.tests)
    rep_tok = 0
    if not passed:                                    # ONE cheap repair, then give up
        code, rep_tok = complete(repair_prompt(spec, code, out), cheap)
        passed, _ = run_pytest(code, spec.tests)
    oracle_passed = run_pytest(code, task.oracle_tests)[0] if task.oracle_tests else None
    outcome = Outcome(task.task_id, warm, passed, bool(hit), spec_tok, impl_tok, rep_tok,
                      oracle_passed)
    if warm and not hit and passed:
        memory.put(task, to_template(spec, task.params))      # ACCEPT only
    return outcome
```

## Stages — prompts must force SINGLE-SHOT output (Codex is an agent)
Codex will otherwise run commands / edit files / take extra turns = extra tokens + latency. Every prompt ends with: **"Output only <the artifact>. Do not run commands, do not edit files, do not explain."**
- `spec(task)` → returns JSON `{signature, template, tests}` with `{param}` placeholders. `json.loads`; if not valid JSON, return a failed `Outcome` with the spec tokens and make no further model calls.
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
- `render(spec, params)`: replace only known `{key}` placeholders in signature/template/tests. Do not use unrestricted `str.format`, because Python dict/set braces in generated artifacts must remain literal.
- Params are JSON-like. Strings render verbatim; lists, mappings, booleans, numbers, and null render as sorted, compact JSON. This gives structured values one deterministic representation.
- `Task.oracle_tests` run separately against the final code for evaluation. They are never placed in a model prompt. `Outcome.passed` remains the spec-test result; `Outcome.oracle_passed` carries the hidden-test result.
- A MISS is stored only after its implementation passes the spec tests. Failed attempts never poison memory.

## Cut (don't build)
pyright, ruff, PRIME, structural match, sanity check, independent tests, escalation beyond one repair, any memory logic, any Codex/EverOS/Snowflake specifics (all behind ports). Non-crashing bugs are fine.

## Self-test (no Lane B, no Codex, no EverOS)
```python
solve(task, warm=False, complete=lambda p,m:(CANNED_CODE,120), memory=NoMemory(), strong="s", cheap="c")
```
Assert: cold runs and returns an Outcome; deliberately-failing canned code triggers exactly one repair; with a real dict `memory`, `reused` is True on the 2nd task whose `get()` returns the stored spec.

## Merge
Lane B injects its real `complete` (Codex) and `memory` (EverOS) into your `solve` unchanged. Nothing in `rrc/pipeline/` should need edits.
