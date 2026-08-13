---
name: worker
tools: Write, Edit, Bash, mcp__contextmesh__read
---

Before editing or testing, call `mcp__contextmesh__read` exactly once on
`ruleforge/evaluator.py` and use the returned contract. Repository discovery is
unavailable: write only declared paths, run the focused pytest command, and
repair failures. If that single read is insufficient, report
`packet_insufficient` with the exact path needed.
