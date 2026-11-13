"""Safe, sandboxed expression language for rule conditions.

Rules are written by business users in YAML, for example::

    condition: "credit.score >= params.min_credit_score"

``eval`` is not used. Expressions are parsed with Python's
``ast`` module and walked by a whitelist interpreter:

* Only the node types in ``_ALLOWED_NODES`` are accepted.
* Names resolve against a read-only context (dict of dicts), never against
  Python objects, so there is no attribute access to ``__class__`` etc.
* Only whitelisted functions can be called.
* Exponentiation, comprehensions, lambdas, assignments and imports are
  rejected at *compile time*, i.e. when the workflow config is loaded, so a
  bad expression can never be activated.
* Length, depth and argument-count checks run at compile time; at runtime a
  step budget, size caps on sequence repetition and concatenation, a number
  magnitude cap and regex restrictions bound the cost of a single evaluation.

Every evaluation records which variables were read and their values. That
"data reference" list is what makes each decision explainable.
"""

from __future__ import annotations

import ast
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

MAX_EXPRESSION_LENGTH = 500
MAX_AST_DEPTH = 32
MAX_EVAL_STEPS = 5_000
MAX_REGEX_INPUT = 1_000

# Nested quantifiers such as (a+)+ or (a*)* cause catastrophic backtracking in
# Python's regex engine (ReDoS). Python's `re` has no timeout, so such patterns
# are refused when the workflow is loaded. A production system would use RE2.
_NESTED_QUANTIFIER = re.compile(r"\((?:[^()\\]|\\.)*[+*}](?:[^()\\]|\\.)*\)\s*[+*{]")


def check_regex(pattern: Any) -> re.Pattern[str]:
    """Validate a regex used by matches(): must compile, be short, and have no nested quantifiers."""
    if not isinstance(pattern, str):
        raise ExpressionError("regex pattern must be a string")
    if len(pattern) > 200:
        raise ExpressionError("regex pattern too long")
    if _NESTED_QUANTIFIER.search(pattern):
        raise ExpressionError(f"regex {pattern!r} has nested quantifiers (catastrophic backtracking risk)")
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ExpressionError(f"invalid regex {pattern!r}: {exc}") from None


class ExpressionError(ValueError):
    """Raised when an expression is syntactically invalid or uses forbidden constructs."""


class MissingDataError(LookupError):
    """Raised at evaluation time when a referenced variable is absent from the context."""

    def __init__(self, path: str):
        super().__init__(f"missing data: {path}")
        self.path = path


class _Missing:
    """Sentinel for variables that are absent. Only ``exists()`` may observe it."""

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "<missing>"


MISSING = _Missing()

_ALLOWED_NODES: tuple[type, ...] = (
    ast.Expression,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.UnaryOp,
    ast.Not,
    ast.USub,
    ast.UAdd,
    ast.BinOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Mod,
    ast.FloorDiv,
    ast.Compare,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
    ast.Is,
    ast.IsNot,
    ast.IfExp,
    ast.Call,
    ast.Name,
    ast.Load,
    ast.Attribute,
    ast.Subscript,
    ast.Constant,
    ast.List,
    ast.Tuple,
)


def _to_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    raise ExpressionError(f"cannot interpret {value!r} as a date")


@dataclass
class EvalContext:
    """Variables visible to expressions plus a deterministic 'today'.

    ``today`` is fixed per request and recorded in the audit trail, so a
    decision can be replayed later and produce the identical result.
    """

    variables: dict[str, Any]
    today: date
    reads: dict[str, Any] = field(default_factory=dict)

    def resolve(self, path: str) -> Any:
        node: Any = self.variables
        for part in path.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                self.reads[path] = None
                return MISSING
        self.reads[path] = node
        return node


def _functions(ctx: EvalContext) -> dict[str, Callable[..., Any]]:
    def exists(value: Any) -> bool:
        return value is not MISSING and value is not None

    def days_since(value: Any) -> int:
        return (ctx.today - _to_date(value)).days

    def days_until(value: Any) -> int:
        return (_to_date(value) - ctx.today).days

    def matches(value: Any, pattern: str) -> bool:
        if not isinstance(value, str):
            return False
        if len(value) > MAX_REGEX_INPUT:
            raise ExpressionError("value too long for regex matching")
        return check_regex(pattern).fullmatch(value) is not None

    def safe_sum(items: Any) -> Any:
        if not isinstance(items, list) or not all(
            isinstance(i, (int, float)) and not isinstance(i, bool) for i in items
        ):
            raise ExpressionError("sum() accepts a list of numbers only")
        return sum(items)

    def count_where(items: Any, key: str, expected: Any) -> int:
        if not isinstance(items, list):
            return 0
        return sum(1 for it in items if isinstance(it, dict) and it.get(key) == expected)

    def all_where(items: Any, key: str, expected: Any) -> bool:
        return (
            isinstance(items, list)
            and len(items) > 0
            and all(isinstance(it, dict) and it.get(key) == expected for it in items)
        )

    def pluck(items: Any, key: str) -> list[Any]:
        if not isinstance(items, list):
            return []
        return [it.get(key) for it in items if isinstance(it, dict)]

    def safe_div(a: Any, b: Any, default: Any = 0) -> Any:
        return default if not b else a / b

    return {
        "exists": exists,
        "len": len,
        "abs": abs,
        "min": min,
        "max": max,
        "round": round,
        "sum": safe_sum,
        "lower": lambda s: s.lower() if isinstance(s, str) else s,
        "upper": lambda s: s.upper() if isinstance(s, str) else s,
        "days_since": days_since,
        "days_until": days_until,
        "matches": matches,
        "count_where": count_where,
        "all_where": all_where,
        "pluck": pluck,
        "safe_div": safe_div,
        "floor": math.floor,
        "ceil": math.ceil,
    }


FUNCTION_NAMES = frozenset(_functions(EvalContext({}, date.today())).keys())

# (min_args, max_args) checked when the workflow is compiled.
FUNCTION_ARITY: dict[str, tuple[int, int]] = {
    "exists": (1, 1),
    "len": (1, 1),
    "abs": (1, 1),
    "min": (1, 10),
    "max": (1, 10),
    "round": (1, 2),
    "sum": (1, 1),
    "lower": (1, 1),
    "upper": (1, 1),
    "days_since": (1, 1),
    "days_until": (1, 1),
    "matches": (2, 2),
    "count_where": (3, 3),
    "all_where": (3, 3),
    "pluck": (2, 2),
    "safe_div": (2, 3),
    "floor": (1, 1),
    "ceil": (1, 1),
}
assert set(FUNCTION_ARITY) == FUNCTION_NAMES
_LITERAL_NAMES = {"true": True, "false": False, "null": None, "True": True, "False": False, "None": None}


def _dotted_name(node: ast.AST) -> str | None:
    """Return 'a.b.c' for Name/Attribute chains, else None."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


@dataclass(frozen=True)
class CompiledExpression:
    source: str
    tree: ast.Expression
    variables: frozenset[str]
    regex_params: frozenset[str] = frozenset()  # params.<x> used as regex patterns; validated by the compiler

    def evaluate(self, ctx: EvalContext) -> Any:
        return _Interpreter(ctx).visit(self.tree.body)


def compile_expression(source: str, allowed_roots: set[str] | None = None) -> CompiledExpression:
    """Parse and statically validate an expression. Raises ExpressionError."""
    if not isinstance(source, str) or not source.strip():
        raise ExpressionError("expression must be a non-empty string")
    if len(source) > MAX_EXPRESSION_LENGTH:
        raise ExpressionError(f"expression longer than {MAX_EXPRESSION_LENGTH} characters")
    try:
        tree = ast.parse(source.strip(), mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"syntax error in {source!r}: {exc.msg}") from exc

    variables: set[str] = set()
    regex_params: set[str] = set()

    def check(node: ast.AST, depth: int) -> None:
        if depth > MAX_AST_DEPTH:
            raise ExpressionError("expression nested too deeply")
        if not isinstance(node, _ALLOWED_NODES):
            raise ExpressionError(f"forbidden construct '{type(node).__name__}' in {source!r}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ExpressionError(f"private attribute access is not allowed in {source!r}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTION_NAMES:
                raise ExpressionError(f"unknown or forbidden function in {source!r}")
            if node.keywords:
                raise ExpressionError("keyword arguments are not supported")
            lo, hi = FUNCTION_ARITY[node.func.id]
            if not lo <= len(node.args) <= hi:
                raise ExpressionError(f"{node.func.id}() takes {lo}-{hi} arguments, got {len(node.args)} in {source!r}")
            if node.func.id == "matches":
                pat = node.args[1]
                if isinstance(pat, ast.Constant):
                    check_regex(pat.value)
                elif (_dotted_name(pat) or "").startswith("params.") and _dotted_name(pat).count(".") == 1:  # type: ignore[union-attr]
                    regex_params.add(_dotted_name(pat).split(".")[1])  # type: ignore[union-attr]
                else:
                    raise ExpressionError("matches() pattern must be a string literal or a params.<name> reference")
            for arg in node.args:
                check(arg, depth + 1)
            return
        if isinstance(node, ast.Attribute) and _dotted_name(node) is None:
            # field access on a computed value, e.g. input.documents[0].issuing_country
            check(node.value, depth + 1)
            return
        if isinstance(node, (ast.Name, ast.Attribute)):
            dotted = _dotted_name(node)
            if dotted is None:  # pragma: no cover - handled above
                raise ExpressionError(f"unsupported attribute access in {source!r}")
            if dotted in _LITERAL_NAMES:
                return
            root = dotted.split(".")[0]
            if allowed_roots is not None and root not in allowed_roots:
                raise ExpressionError(f"unknown variable root '{root}' in {source!r}; allowed: {sorted(allowed_roots)}")
            variables.add(dotted)
            return
        if isinstance(node, ast.Constant) and isinstance(node.value, (bytes, complex)):
            raise ExpressionError("unsupported literal type")
        if isinstance(node, ast.Compare):
            for op, comp in zip(node.ops, node.comparators, strict=True):
                if isinstance(op, (ast.Is, ast.IsNot)) and not (
                    (isinstance(comp, ast.Constant) and comp.value is None)
                    or (isinstance(comp, ast.Name) and comp.id in ("None", "null"))
                ):
                    raise ExpressionError(f"'is' / 'is not' may only compare with None in {source!r}")
        for child in ast.iter_child_nodes(node):
            check(child, depth + 1)

    check(tree, 0)
    return CompiledExpression(
        source=source.strip(), tree=tree, variables=frozenset(variables), regex_params=frozenset(regex_params)
    )


MAX_SEQUENCE_RESULT = 10_000
MAX_NUMBER_MAGNITUDE = 10**18


def _guarded_mult(a: Any, b: Any) -> Any:
    """Block memory/CPU blow-ups such as 'a' * 10**9 or huge integer products."""
    if isinstance(a, (str, list)) or isinstance(b, (str, list)):
        seq, n = (a, b) if isinstance(a, (str, list)) else (b, a)
        if not isinstance(n, int) or len(seq) * max(n, 0) > MAX_SEQUENCE_RESULT:
            raise ExpressionError("sequence repetition exceeds the allowed size")
    result = a * b
    if isinstance(result, int) and not isinstance(result, bool) and abs(result) > MAX_NUMBER_MAGNITUDE:
        raise ExpressionError("numeric result exceeds the allowed magnitude")
    return result


def _guarded_mod(a: Any, b: Any) -> Any:
    if isinstance(a, (str, bytes)):
        raise ExpressionError("string formatting with % is not allowed")
    return a % b


def _guarded_add(a: Any, b: Any) -> Any:
    if isinstance(a, (str, list)) and isinstance(b, (str, list)) and len(a) + len(b) > MAX_SEQUENCE_RESULT:
        raise ExpressionError("sequence concatenation exceeds the allowed size")
    return a + b


_BIN_OPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: _guarded_add,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: _guarded_mult,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: _guarded_mod,
}

_CMP_OPS: dict[type, Callable[[Any, Any], bool]] = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
    ast.Is: lambda a, b: a is b,
    ast.IsNot: lambda a, b: a is not b,
}


class _Interpreter:
    def __init__(self, ctx: EvalContext):
        self.ctx = ctx
        self.funcs = _functions(ctx)
        self.steps = 0

    def value(self, node: ast.AST) -> Any:
        """Evaluate and fail loudly if a variable is missing."""
        result = self.visit(node)
        if result is MISSING:
            raise MissingDataError(_dotted_name(node) or ast.unparse(node))
        return result

    def visit(self, node: ast.AST) -> Any:
        self.steps += 1
        if self.steps > MAX_EVAL_STEPS:
            raise ExpressionError("evaluation step budget exceeded")
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Attribute) and _dotted_name(node) is None:
            container = self.value(node.value)
            if isinstance(container, dict) and node.attr in container:
                return container[node.attr]
            raise MissingDataError(ast.unparse(node))
        if isinstance(node, (ast.Name, ast.Attribute)):
            dotted = _dotted_name(node)
            assert dotted is not None
            if dotted in _LITERAL_NAMES:
                return _LITERAL_NAMES[dotted]
            return self.ctx.resolve(dotted)
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                result: Any = True
                for v in node.values:
                    result = self.value(v)
                    if not result:
                        return result
                return result
            result = False
            for v in node.values:
                result = self.value(v)
                if result:
                    return result
            return result
        if isinstance(node, ast.UnaryOp):
            operand = self.value(node.operand)
            if isinstance(node.op, ast.Not):
                return not operand
            if isinstance(node.op, ast.USub):
                return -operand
            return +operand
        if isinstance(node, ast.BinOp):
            return _BIN_OPS[type(node.op)](self.value(node.left), self.value(node.right))
        if isinstance(node, ast.Compare):
            left = self.value(node.left)
            for op, comparator in zip(node.ops, node.comparators, strict=True):
                right = self.value(comparator)
                if not _CMP_OPS[type(op)](left, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.IfExp):
            return self.value(node.body) if self.value(node.test) else self.value(node.orelse)
        if isinstance(node, ast.Call):
            assert isinstance(node.func, ast.Name)
            name = node.func.id
            if name == "exists":
                # exists() is the only function allowed to observe a missing value.
                return self.funcs["exists"](self.visit(node.args[0]))
            return self.funcs[name](*[self.value(a) for a in node.args])
        if isinstance(node, ast.Subscript):
            container = self.value(node.value)
            key = self.value(node.slice)
            try:
                return container[key]
            except (KeyError, IndexError, TypeError):
                raise MissingDataError(ast.unparse(node)) from None
        if isinstance(node, (ast.List, ast.Tuple)):
            return [self.value(e) for e in node.elts]
        raise ExpressionError(f"cannot evaluate {type(node).__name__}")  # pragma: no cover


TEMPLATE_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_.]*)\}")


def render_template(template: str, ctx: EvalContext) -> str:
    """Render 'Score {credit.score} below {params.min_score}' using context values."""

    def repl(match: re.Match[str]) -> str:
        val = ctx.resolve(match.group(1))
        return "<missing>" if val is MISSING else str(val)

    return TEMPLATE_RE.sub(repl, template)
