# Project working workflow

The current Codex parent is the project orchestrator: it scopes work, owns
integration decisions, creates disjoint worktrees/branches when needed, and
verifies the combined result. Do not replace it with a native collaboration
subagent for implementation work.

All delegated project coding, review, and test work uses an external Codex
DeepSeek V4 Flash worker, not a host collaboration subagent. Launch it
through `.codex/scripts/Invoke-DeepSeekDelegate.ps1`; that launcher is bound to
the checked-in `deepseek-v4-flash:0731-cloud` Ollama profile, High reasoning,
a 1,048,576-token context window, and a 230,000-token automatic-compaction
threshold. It always uses full Codex access.

Give each independent delegated work slice a unique `SessionKey`. Reuse that
key only to resume the same slice, so its retained `.codex/delegates/sessions`
thread id, JSONL stream, and final handoff remain private to that worker.
Never share a session key between concurrent workers. Start a worker with:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\.codex\scripts\Invoke-DeepSeekDelegate.ps1 `
  -SessionKey <lane-id> `
  '<specific scoped task and verification command>'
```

For concurrent work, the parent assigns non-overlapping objectives and write
ownership before launching workers. Production edits and integration stay
serial unless the parent explicitly creates isolated worktrees for genuinely
independent slices. Do not launch model work merely to inspect, plan, or ask a
question; use the worker only for an authorized delegated task.
