# rrc/contract.py  —  FROZEN shared seam. Both lanes import from here.
# Editing this file is a coordination sync-point: stop, agree, edit once, both re-pull.
# Keep it this small. No arms enum, no ModelPort class, no invariants ceremony.

from dataclasses import dataclass
from typing import Optional, Protocol, Callable


@dataclass
class Task:
    task_id: str
    family: str          # retrieval key; also the EverOS session_id (the join trick)
    params: dict         # slot values, e.g. {"entity": "Order", "fields": ["id", "total"]}
    text: str            # NL description; fed to Codex AND embedded by EverOS for matching
    oracle_tests: str    # EVAL ONLY — never shown to a model


@dataclass
class Spec:              # stored as templates so reuse is str.format(), not NLP
    signature: str
    template: str        # spec body with {param} placeholders
    tests: str           # pytest snippet with {param} placeholders


@dataclass
class Outcome:
    task_id: str
    warm: bool
    passed: bool
    reused: bool
    spec_tokens: int
    impl_tokens: int
    repair_tokens: int

    @property
    def total(self) -> int:
        return self.spec_tokens + self.impl_tokens + self.repair_tokens


# model call -> (text, total_tokens). Lane B shells to `codex exec`; Lane A calls it.
Complete = Callable[[str, str], tuple[str, int]]     # (prompt, model) -> (text, tokens)


class Memory(Protocol):  # Lane B implements with EverOS; Lane A calls
    def get(self, task: "Task") -> Optional[Spec]: ...    # semantic match on task.text
    def put(self, task: "Task", spec: Spec) -> None: ...


class NoMemory:          # cold arm (shared)
    def get(self, task: "Task") -> Optional[Spec]:
        return None

    def put(self, task: "Task", spec: Spec) -> None:
        pass


# Lane A implements this; Lane B injects `complete` and `memory` into it.
def solve(task: Task, *, warm: bool, complete: Complete, memory: Memory,
          strong: str, cheap: str) -> Outcome:
    ...
