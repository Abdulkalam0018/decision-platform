"""Sandboxed expression language: correctness, data references, and safety."""

from datetime import date

import pytest

from decision_platform.rules.expression import (
    EvalContext,
    ExpressionError,
    MissingDataError,
    compile_expression,
    render_template,
)

TODAY = date(2026, 11, 13)
VARS = {
    "input": {
        "age": 30,
        "amount": 1000,
        "country": "IN",
        "tags": ["a", "b"],
        "docs": [{"type": "passport", "exp": "2027-01-01"}, {"type": "bill", "exp": "2026-12-01"}],
        "opt": None,
    },
    "params": {"min_age": 18, "countries": ["IN", "US"]},
    "credit": {"score": 700},
}


def ev(src, variables=VARS):
    ctx = EvalContext(variables, TODAY)
    return compile_expression(src).evaluate(ctx), ctx


@pytest.mark.parametrize(
    "src,expected",
    [
        ("input.age >= params.min_age", True),
        ("input.amount / 4 == 250", True),
        ("input.amount % 7", 1000 % 7),
        ("18 <= input.age < 65", True),
        ("input.country in params.countries", True),
        ("input.country not in ['US']", True),
        ("not (input.age < 18)", True),
        ("input.age > 40 or credit.score >= 700", True),
        ("'high' if credit.score > 650 else 'low'", "high"),
        ("len(input.tags)", 2),
        ("max(1, input.age, 5)", 30),
        ("count_where(input.docs, 'type', 'passport')", 1),
        ("min(pluck(input.docs, 'exp'))", "2026-12-01"),
        ("days_until('2026-11-20')", 7),
        ("days_since('2026-11-03')", 10),
        ("matches('24AAACA1234F1Z5', '^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$')", True),
        ("input.docs[0].type", "passport"),
        ("exists(input.missing_field)", False),
        ("exists(input.opt)", False),
        ("input.opt is None", True),
        ("safe_div(10, 0, -1)", -1),
        ("true and not false", True),
    ],
)
def test_evaluates_correctly(src, expected):
    assert ev(src)[0] == expected


def test_records_data_references_for_explainability():
    _, ctx = ev("input.age >= params.min_age and credit.score > 600")
    assert ctx.reads == {"input.age": 30, "params.min_age": 18, "credit.score": 700}


def test_missing_variable_raises_missing_data():
    with pytest.raises(MissingDataError) as exc:
        ev("input.income > 0")
    assert exc.value.path == "input.income"


def test_missing_list_index_is_missing_data():
    with pytest.raises(MissingDataError):
        ev("input.docs[5].type == 'x'")


def test_short_circuit_avoids_missing_data():
    assert ev("exists(input.income) and input.income > 0")[0] is False


@pytest.mark.parametrize(
    "src",
    [
        "__import__('os').system('echo pwned')",
        "input.__class__",
        "input._private",
        "(lambda: 1)()",
        "2 ** 10000",
        "[x for x in input.tags]",
        "open('/etc/passwd')",
        "eval('1')",
        "x := 1",
        "input.age.bit_length()",
        "getattr(input, 'age')",
        "len(s=1)",
        "b'bytes'",
        "a" * 501,
        "not " * 40 + "true",
    ],
)
def test_forbidden_constructs_rejected_at_compile_time(src):
    with pytest.raises(ExpressionError):
        compile_expression(src)


def test_unknown_root_rejected_when_roots_restricted():
    with pytest.raises(ExpressionError, match="unknown variable root"):
        compile_expression("secrets.key == 1", allowed_roots={"input", "params"})


def test_syntax_error_reported():
    with pytest.raises(ExpressionError, match="syntax error"):
        compile_expression("input.age >=")


def test_compile_collects_variables():
    ce = compile_expression("input.a + params.b > credit.score")
    assert ce.variables == {"input.a", "params.b", "credit.score"}


def test_type_error_propagates_for_rule_engine_to_handle():
    with pytest.raises(TypeError):
        ev("input.country > 5")


def test_render_template():
    ctx = EvalContext(VARS, TODAY)
    assert render_template("Score {credit.score} vs {params.min_age} {input.nope}", ctx) == "Score 700 vs 18 <missing>"


@pytest.mark.parametrize(
    "src",
    [
        "'a' * 1000000000",
        "[1] * 100000000",
        "999999999999 * 999999999999 * 999999999999",
    ],
)
def test_resource_exhaustion_blocked_at_runtime(src):
    with pytest.raises(ExpressionError):
        ev(src)


def test_small_repetition_still_allowed():
    assert ev("'ab' * 3")[0] == "ababab"
