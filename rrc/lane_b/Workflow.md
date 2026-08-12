# Lane B Workflow

> **Historical / non-RRCv2 / superseded.** This workflow is retained only for reproducibility; it is
> not current product guidance. ADR 0002 and `docs/RRCv2.md` govern the active implementation. The
> old coding-agent topology below is fixture-only/non-product.

## Coding topology — not the product runtime

Use one expensive **coding orchestrator** and a fresh `luna-xhigh-fast`
**coding subagent** for each bounded coding task. The default is exactly one
active subagent. Do not use a multi-agent harness or specialist review/test
agents.

```text
coding orchestrator (plans, scopes, reviews code)
    -> fresh Luna coding subagent (reads and edits only)
    -> structured code-result packet
    -> agent exits; orchestrator approves or sends the next coding packet
```

This loop is for code changes only. Neither the coding orchestrator nor Luna
calls EverOS, runs tests, starts the RRC runner, invokes a real model/subagent,
or performs product validation. They review plans and diffs only. The coding
orchestrator is not the RRC runtime identity described below.

Every delegation follows [SUBAGENT_PROTOCOL.md](SUBAGENT_PROTOCOL.md). The
orchestrator sends a fresh, self-contained packet every time: one plan, one
implementation spec, explicit write paths, and at most five initial files.

## Continuity policy

Every coding task returns a result. Orchestrator, packet, process-scaffold,
tool, or evidence-collection errors are non-blocking operational errors:
record them, use the smallest direct coding fallback, and send the next useful
coding packet. They never stop the model.

A coding review can report a possible product risk, but cannot call it a
confirmed Lane B defect because it does not execute the product. Real tests
are outside this loop.

## User test gate

When the next step needs a real EverOS call, the RRC runner, a test command,
or a real validation subagent, the coding loop stops. It returns a concise test
handoff: changed files, the proposed live checks, required services/cost, and
known risks. **The user decides whether and how to run that test.** No real
testing agent is selected or launched automatically.

If the user authorizes validation, only a reproduced normal-path Lane B defect
may prevent the associated validation claim: for example, a wrong case-index
write/search, `external_ref` join, namespace boundary, or rendered template.
Test, harness, and process errors are reported as test infrastructure issues,
not product defects.

## Parallelization state

Stay serial until the coding orchestrator explicitly declares `Execution mode:
parallel` in task packets. It may do so only for disjoint write paths,
independent acceptance criteria, no shared runtime state, and a defined
integration order. Each parallel task still gets a fresh small-context Luna
subagent. Otherwise the next coding subagent starts only after code review.

## Product architecture being coded

Lane B has two runtime storage systems:

```text
Lane A accepted Template
    -> SQLite exact-template store (RRC-owned)
    -> EverOS runtime case index (external_ref metadata)

new Task
    -> runtime case-alignment search returns external_ref
    -> SQLite returns exact Template
    -> Lane A renders it with this task's slot values
```

EverOS is never the artifact store. It finds an `external_ref`; SQLite owns the
generic template skeleton. The full template contains the plan, signature,
contract, tests, and slot schema, not a rendered task instance.

## Runtime EverOS boundaries

These identifiers belong to the future product runtime, not to the coding
orchestrator or Luna:

```text
RRC case index: app=reasonrender, project=rrc-template-index,
                user=rrc-runtime
project memory: app=reasonrender, project=orchestrator-memory,
                user=product-runtime
```

The first is a compact library of prior coding cases and is the only source
for `external_ref` retrieval. The second is long-horizon project memory and is
never queried by RRC. The synchronous episode track is the case-alignment MVP;
do not add async `agent_case`/`agent_skill` work. A future real runtime
subagent receives only its selected template/spec, never a general EverOS dump.

## Coding slice sequence

1. Luna edits the EverOS metadata-path source and prepares its validation
   handoff; it does not start EverOS or run the patch test.
2. Luna implements the SQLite store and retrieval join from the supplied
   contract/spec; it does not run the round trip.
3. Luna implements the structured workload, runner, and model adapter; it does
   not invoke a model, solver, test runner, or Snowflake.
4. The coding orchestrator reviews the completed diffs and stops at the user
   test gate with the proposed validation sequence.

## Ownership

Lane B owns the small EverOS `external_ref` patch, SQLite storage by
`Template.external_ref`, the EverOS/SQLite `RetrievalPort`, the Codex
`ModelPort`, workload, COLD/WARM runner, and Snowflake rows. Lane A owns SPEC
generation, slot values, `templatize()`, structural matching, rendering,
pytest, and the decision to store only an accepted template.

## Fast decisions

- Use only exact template reuse. NEAR candidates are MISS; no PRIME.
- Search only `rrc-template-index`; project-memory hits are not candidates.
- Ignore a search hit with no `external_ref` or no SQLite row; it is a MISS.
- Keep the external-ref patch. The old family/dictionary shortcut is not a
  valid replacement for this architecture.
- Real runtime debugging and tests require the user test gate; coding agents
  only leave concise handoff notes for them.
