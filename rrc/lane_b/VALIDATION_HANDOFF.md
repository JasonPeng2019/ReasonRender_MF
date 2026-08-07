# Lane B Validation Handoff

## Status

The coding slices are complete. No live service, model, subprocess, test, or
Snowflake operation has been run. This document is the required user test gate.

## Preconditions

1. Start EverOS from the checked-out `EverOS` submodule with its patched code.
   If its existing LanceDB `episode` table rejects the new `external_ref`
   column, use EverOS's normal cascade rebuild/recovery procedure before the
   proof. Do not silently ignore a schema error.
2. Ensure the Codex CLI is authenticated and select a model available to that
   CLI account.
3. Confirm that `http://127.0.0.1:8000/health` reports a healthy cascade.

## Proposed user-authorized proof

From the repository root, set the model and run exactly one two-task proof:

```powershell
$env:RRC_MODEL = "<available-codex-model>"
$env:RRC_EVEROS_URL = "http://127.0.0.1:8000"
python -m rrc.run
```

The command creates an ignored `runtime/rrc/rrc-proof-*/` directory containing
`templates.sqlite` and `evidence.json`. It calls EverOS and two real Codex
completions, so it requires the user's explicit approval before execution.

## Expected evidence

`evidence.json` must have `pass: true`, with all of the following:

- first outcome: `reused: false`, passed its generated tests and oracle;
- second outcome: `reused: true`, passed its generated tests and oracle, and
  `spec_tokens: 0`;
- `stored_external_ref` equals `retrieved_external_ref`;
- `selected_template` is generic (contains its named slots) and
  `rendered_second_spec` contains the second task's values;
- the second task was found only through the fixed
  `reasonrender/rrc-template-index/rrc-runtime` EverOS namespace.

The proof starts with `warm=True` deliberately: this makes the first task an
attempted retrieval that must MISS and then seed the reusable template. The
evidence distinguishes that COLD/MISS seed with `reused: false`.

## Failure handling

EverOS availability, indexing/readiness, schema migration, CLI authentication,
or test-process failures are validation-infrastructure evidence, not confirmed
Lane B defects. Capture the command output and `evidence.json` if present, then
choose the next validation action. A Lane B defect is confirmed only by a
minimal reproduction of an incorrect normal path: case-index write/search,
`external_ref` join, namespace isolation, or rendered dynamic template.

## Known unvalidated risks

- Existing EverOS LanceDB data may need its normal rebuild after the schema
  change.
- The fixed similarity threshold (`0.3`) is intentionally uncalibrated until
  the live two-task proof.
- Codex JSONL parsing and the selected CLI model have not been exercised.
- Snowflake is not part of this minimum ship proof because no mandatory record
  target or credentials were supplied.
