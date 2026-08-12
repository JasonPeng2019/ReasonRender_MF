"""Pure positive-language policy for the controlled RRCv2 synthetic profile.

This module deliberately has no provider, store, orchestration, or verifier imports.  It is the
single parser used by preregistration and, after the contract activation gate, by the synthetic
verifier.  General-profile Python must never be routed through this policy.
"""

from __future__ import annotations

import ast
import keyword
import unicodedata
from dataclasses import dataclass
from enum import Enum

MAX_SOURCE_BYTES = 1_048_576
MAX_AST_NODES = 20_000
MAX_AST_DEPTH = 64

_SAFE_BUILTINS = frozenset(
    {
        "abs",
        "all",
        "any",
        "bool",
        "dict",
        "enumerate",
        "float",
        "int",
        "len",
        "list",
        "max",
        "min",
        "range",
        "reversed",
        "round",
        "set",
        "sorted",
        "str",
        "sum",
        "tuple",
        "zip",
    }
)
_SAFE_EXCEPTIONS = frozenset(
    {
        "AssertionError",
        "ValueError",
        "TypeError",
        "KeyError",
        "IndexError",
        "ZeroDivisionError",
        "OverflowError",
    }
)
_SAFE_FACADE_CALLS = {
    "math": frozenset({"ceil", "floor", "sqrt", "isqrt", "gcd", "lcm", "fabs", "isclose"}),
    "re": frozenset({"fullmatch", "match", "search", "sub", "split", "escape"}),
    "json": frozenset({"loads", "dumps"}),
}
_SAFE_VALUE_METHODS = frozenset(
    {
        "append",
        "add",
        "extend",
        "get",
        "items",
        "keys",
        "values",
        "strip",
        "rstrip",
        "isalnum",
        "isalpha",
        "lower",
        "upper",
        "split",
        "join",
        "startswith",
        "endswith",
        "replace",
        "sort",
    }
)
_DENIED_NAMES = frozenset(
    {
        "__import__",
        "open",
        "compile",
        "eval",
        "exec",
        "globals",
        "locals",
        "vars",
        "dir",
        "getattr",
        "setattr",
        "delattr",
        "ctypes",
        "gc",
        "builtins",
        "pytest",
        "_pytest",
        "os",
        "sys",
        "subprocess",
        "socket",
        "threading",
        "signal",
        "importlib",
    }
)
_SYNTHETIC_TYPE_CONTAINERS = frozenset({"list", "dict", "tuple", "set"})


class PolicyKind(str, Enum):
    CANDIDATE = "candidate"
    TEST = "test"


@dataclass(frozen=True)
class PolicyResult:
    kind: PolicyKind
    source_bytes: int
    ast_nodes: int
    declared_functions: tuple[str, ...]


class SyntheticPolicyError(ValueError):
    """Raised when source is outside the frozen positive language."""


def _public_name(name: str) -> bool:
    return (
        name.isascii()
        and name.isidentifier()
        and not keyword.iskeyword(name)
        and not name.startswith("_")
    )


def _validate_text(source: str) -> bytes:
    if not isinstance(source, str) or not source:
        raise SyntheticPolicyError("source must be a nonempty string")
    if source != unicodedata.normalize("NFC", source):
        raise SyntheticPolicyError("source must already be NFC")
    if "\r" in source or "\x00" in source:
        raise SyntheticPolicyError("source must use LF-only text without NUL")
    raw = source.encode("utf-8", errors="strict")
    if len(raw) > MAX_SOURCE_BYTES:
        raise SyntheticPolicyError("source exceeds byte cap")
    return raw


def _tree_depth(node: ast.AST) -> int:
    children = tuple(ast.iter_child_nodes(node))
    return 1 if not children else 1 + max(_tree_depth(child) for child in children)


def _literal(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        value = node.value
        if value is None or isinstance(value, bool):
            return True
        if isinstance(value, int):
            return -(2**63) <= value <= 2**63 - 1
        if isinstance(value, str):
            return value == unicodedata.normalize("NFC", value)
        return False
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(_literal(item) for item in node.elts)
    if isinstance(node, ast.Dict):
        return all(
            key is not None and _literal(key) and _literal(value)
            for key, value in zip(node.keys, node.values, strict=True)
        )
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return (
            isinstance(node.operand, ast.Constant)
            and isinstance(node.operand.value, int)
            and not isinstance(node.operand.value, bool)
            and 1 <= node.operand.value <= 2**63
        )
    return False


def _type_annotation(node: ast.AST, *, depth: int = 0) -> bool:
    if depth > 8:
        return False
    if isinstance(node, ast.Name):
        return _public_name(node.id) and node.id not in _DENIED_NAMES
    if isinstance(node, ast.Constant):
        return node.value is None
    if isinstance(node, ast.Attribute):
        segments: list[str] = []
        current: ast.AST = node
        while isinstance(current, ast.Attribute):
            segments.append(current.attr)
            current = current.value
        return (
            isinstance(current, ast.Name)
            and all(_public_name(segment) for segment in (current.id, *reversed(segments)))
            and current.id not in _DENIED_NAMES
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _type_annotation(node.left, depth=depth + 1) and _type_annotation(
            node.right, depth=depth + 1
        )
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
        if node.value.id not in _SYNTHETIC_TYPE_CONTAINERS:
            return False
        arguments = node.slice.elts if isinstance(node.slice, ast.Tuple) else (node.slice,)
        return bool(arguments) and all(
            _type_annotation(argument, depth=depth + 1) for argument in arguments
        )
    return False


_CANDIDATE_STMTS = (
    ast.Assign,
    ast.AnnAssign,
    ast.AugAssign,
    ast.Expr,
    ast.Return,
    ast.If,
    ast.For,
    ast.While,
    ast.Break,
    ast.Continue,
    ast.Pass,
    ast.Raise,
    ast.Try,
    ast.Assert,
)
_EXPRS = (
    ast.Constant,
    ast.Name,
    ast.Attribute,
    ast.Call,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.IfExp,
    ast.Subscript,
    ast.Slice,
    ast.List,
    ast.Tuple,
    ast.Set,
    ast.Dict,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
    ast.JoinedStr,
    ast.FormattedValue,
)


class _Validator(ast.NodeVisitor):
    def __init__(self, kind: PolicyKind, target: str | None) -> None:
        self.kind = kind
        self.target = target
        self.functions: set[str] = set()
        self.scope_depth = 0
        self.target_calls = 0
        self.expectations = 0
        self._allowed_facade_attributes: set[int] = set()
        self._allowed_facade_names: set[int] = set()

    def generic_visit(self, node: ast.AST) -> None:
        forbidden = (
            ast.Import,
            ast.ImportFrom,
            ast.ClassDef,
            ast.Lambda,
            ast.AsyncFunctionDef,
            ast.Await,
            ast.Yield,
            ast.YieldFrom,
            ast.With,
            ast.AsyncWith,
            ast.AsyncFor,
            ast.Match,
            ast.Global,
            ast.Nonlocal,
            ast.Delete,
            ast.NamedExpr,
        )
        if isinstance(node, forbidden):
            raise SyntheticPolicyError(f"forbidden AST node: {type(node).__name__}")
        super().generic_visit(node)

    def visit_Module(self, node: ast.Module) -> None:
        declared = [stmt.name for stmt in node.body if isinstance(stmt, ast.FunctionDef)]
        if len(declared) != len(set(declared)):
            raise SyntheticPolicyError("duplicate function names are forbidden")
        self.functions.update(declared)
        for stmt in node.body:
            if not isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.FunctionDef)):
                raise SyntheticPolicyError(
                    "top level permits only literal assignments and functions"
                )
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                if self.kind is PolicyKind.TEST:
                    raise SyntheticPolicyError("test modules permit no top-level assignments")
                value = stmt.value
                if value is None or not _literal(value):
                    raise SyntheticPolicyError("top-level assignment must be literal")
            self.visit(stmt)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if self.scope_depth != 0:
            raise SyntheticPolicyError("nested functions are forbidden")
        if not _public_name(node.name):
            raise SyntheticPolicyError("function name must be public ASCII")
        if node.decorator_list or node.type_comment is not None:
            raise SyntheticPolicyError("decorators and type comments are forbidden")
        if node.args.vararg is not None or node.args.kwarg is not None:
            raise SyntheticPolicyError("variadic arguments are forbidden")
        all_args = (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
        if any(not _public_name(arg.arg) for arg in all_args):
            raise SyntheticPolicyError("argument name must be public ASCII")
        if any(
            arg.annotation is not None and not _type_annotation(arg.annotation) for arg in all_args
        ):
            raise SyntheticPolicyError("invalid positive-language parameter annotation")
        if node.returns is not None and not _type_annotation(node.returns):
            raise SyntheticPolicyError("invalid positive-language return annotation")
        defaults = (
            *node.args.defaults,
            *(item for item in node.args.kw_defaults if item is not None),
        )
        if any(not _literal(default) for default in defaults):
            raise SyntheticPolicyError("defaults must be inert literals")
        self.scope_depth += 1
        for stmt in node.body:
            allowed = (ast.Assert, ast.Try) if self.kind is PolicyKind.TEST else _CANDIDATE_STMTS
            if not isinstance(stmt, allowed):
                raise SyntheticPolicyError(f"forbidden function statement: {type(stmt).__name__}")
            self.visit(stmt)
        self.scope_depth -= 1

    def visit_Name(self, node: ast.Name) -> None:
        if node.id.startswith("_") or node.id in _DENIED_NAMES:
            raise SyntheticPolicyError(f"forbidden name: {node.id}")
        if (
            isinstance(node.ctx, ast.Load)
            and node.id in _SAFE_FACADE_CALLS
            and id(node) not in self._allowed_facade_names
        ):
            raise SyntheticPolicyError("facade roots may be used only in direct approved calls")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("_"):
            raise SyntheticPolicyError("private attributes are forbidden")
        current: ast.AST = node
        components: list[str] = []
        while isinstance(current, ast.Attribute):
            components.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name) and current.id in _SAFE_FACADE_CALLS:
            if (
                len(components) != 1
                or id(node) not in self._allowed_facade_attributes
                or components[0] not in _SAFE_FACADE_CALLS[current.id]
            ):
                raise SyntheticPolicyError("facade access must be one approved direct call")
        self.visit(node.value)

    def visit_Call(self, node: ast.Call) -> None:
        if any(isinstance(arg, ast.Starred) for arg in node.args) or any(
            keyword.arg is None for keyword in node.keywords
        ):
            raise SyntheticPolicyError("starred calls are forbidden")
        callee = node.func
        if isinstance(callee, ast.Name):
            candidate_calls = _SAFE_BUILTINS | _SAFE_EXCEPTIONS | self.functions
            if self.kind is PolicyKind.CANDIDATE and callee.id not in candidate_calls:
                raise SyntheticPolicyError(f"undeclared call: {callee.id}")
            if callee.id not in candidate_calls and callee.id != self.target:
                raise SyntheticPolicyError(f"forbidden call: {callee.id}")
            if (
                self.kind is PolicyKind.TEST
                and callee.id != self.target
                and callee.id not in _SAFE_EXCEPTIONS
            ):
                raise SyntheticPolicyError("synthetic tests may call only the target")
        elif isinstance(callee, ast.Attribute):
            if isinstance(callee.value, ast.Name) and callee.value.id in _SAFE_FACADE_CALLS:
                if callee.attr not in _SAFE_FACADE_CALLS[callee.value.id]:
                    raise SyntheticPolicyError("unlisted facade call")
                self._allowed_facade_attributes.add(id(callee))
                self._allowed_facade_names.add(id(callee.value))
            elif callee.attr not in _SAFE_VALUE_METHODS or self.kind is PolicyKind.TEST:
                raise SyntheticPolicyError("unlisted value method")
        else:
            raise SyntheticPolicyError("computed call target is forbidden")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, (bytes, complex)):
            raise SyntheticPolicyError("bytes/complex literals are forbidden")
        if isinstance(node.value, str) and node.value != unicodedata.normalize("NFC", node.value):
            raise SyntheticPolicyError("string literal must be NFC")

    def visit_Raise(self, node: ast.Raise) -> None:
        if (
            node.exc is None
            or not isinstance(node.exc, ast.Call)
            or not isinstance(node.exc.func, ast.Name)
            or node.exc.func.id not in _SAFE_EXCEPTIONS
        ):
            raise SyntheticPolicyError("raise must construct a safe exception")
        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:
        if node.finalbody:
            raise SyntheticPolicyError("finally is forbidden")
        for handler in node.handlers:
            if not isinstance(handler.type, ast.Name) or handler.type.id not in _SAFE_EXCEPTIONS:
                raise SyntheticPolicyError("except must name a safe exception")
        if self.kind is PolicyKind.TEST:
            first = node.body[0] if len(node.body) == 1 else None
            if not (
                len(node.handlers) == 1
                and isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Call)
                and isinstance(first.value.func, ast.Name)
                and first.value.func.id == self.target
                and len(node.orelse) == 1
                and isinstance(node.orelse[0], ast.Raise)
                and isinstance(node.orelse[0].exc, ast.Call)
                and isinstance(node.orelse[0].exc.func, ast.Name)
                and node.orelse[0].exc.func.id == "AssertionError"
            ):
                raise SyntheticPolicyError(
                    "test try must immediately call the target and raise AssertionError on success"
                )
            self.target_calls += 1
            self.expectations += 1
        self.generic_visit(node)

    def visit_Assert(self, node: ast.Assert) -> None:
        if self.kind is PolicyKind.TEST:
            if any(
                isinstance(
                    item,
                    (
                        ast.BoolOp,
                        ast.IfExp,
                        ast.ListComp,
                        ast.SetComp,
                        ast.DictComp,
                        ast.GeneratorExp,
                    ),
                )
                for item in ast.walk(node.test)
            ):
                raise SyntheticPolicyError("lazy or short-circuit assertions are forbidden")
            calls = [
                item
                for item in ast.walk(node.test)
                if isinstance(item, ast.Call)
                and isinstance(item.func, ast.Name)
                and item.func.id == self.target
            ]
            self.target_calls += len(calls)
            self.expectations += 1
        self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        if self.kind is PolicyKind.TEST:
            raise SyntheticPolicyError("boolean short-circuit expressions are forbidden in tests")
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        if self.kind is PolicyKind.TEST:
            raise SyntheticPolicyError("loops are forbidden in synthetic tests")
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        if self.kind is PolicyKind.TEST:
            raise SyntheticPolicyError("loops are forbidden in synthetic tests")
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        if self.kind is PolicyKind.TEST:
            raise SyntheticPolicyError("conditionals are forbidden in synthetic tests")
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:
        if self.kind is PolicyKind.TEST:
            raise SyntheticPolicyError("return is forbidden in synthetic tests")
        self.generic_visit(node)


def validate_synthetic_source(
    source: str, *, kind: PolicyKind | str, target: str | None = None
) -> PolicyResult:
    """Parse and validate one model-authored synthetic candidate or test module."""

    try:
        policy_kind = PolicyKind(kind)
    except ValueError as exc:
        raise SyntheticPolicyError("unknown policy kind") from exc
    raw = _validate_text(source)
    if policy_kind is PolicyKind.TEST and (target is None or not _public_name(target)):
        raise SyntheticPolicyError("test policy requires a public target")
    try:
        tree = ast.parse(source, mode="exec", type_comments=True)
    except (SyntaxError, ValueError) as exc:
        raise SyntheticPolicyError("source does not parse") from exc
    nodes = tuple(ast.walk(tree))
    if len(nodes) > MAX_AST_NODES or _tree_depth(tree) > MAX_AST_DEPTH:
        raise SyntheticPolicyError("AST exceeds cap")
    validator = _Validator(policy_kind, target)
    validator.visit(tree)
    if policy_kind is PolicyKind.TEST:
        defs = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
        if len(defs) != 1 or not defs[0].name.startswith("test_"):
            raise SyntheticPolicyError("test module must contain exactly one test_* function")
        if any(
            not isinstance(node, (ast.FunctionDef, ast.Assign, ast.AnnAssign)) for node in tree.body
        ):
            raise SyntheticPolicyError("invalid test top level")
        if validator.target_calls < 1 or validator.expectations < 1:
            raise SyntheticPolicyError("test must call the target and assert an outcome")
    return PolicyResult(policy_kind, len(raw), len(nodes), tuple(sorted(validator.functions)))


__all__ = [
    "PolicyKind",
    "PolicyResult",
    "SyntheticPolicyError",
    "validate_synthetic_source",
]
