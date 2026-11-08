"""The supplementary audit accepts exact encodings, never rounded or textual data."""

import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "approval_regrade",
    Path(__file__).parents[1] / "benchmarks/approval_model_regrade.py",
)
regrade = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(regrade)


@pytest.mark.parametrize(
    "value,expected",
    [(0, 0), (-123, -123), ("123", 123), ("-123", -123), (str(2**100), 2**100)],
)
def test_exact_integer_encoding(value, expected):
    assert regrade.exact_integer(value) == expected


@pytest.mark.parametrize(
    "value", [True, False, 1.0, "1.0", "1e3", "+1", "01", " 1", None]
)
def test_float_bool_and_ambiguous_spelling_rejected(value):
    with pytest.raises(ValueError):
        regrade.exact_integer(value)


@pytest.mark.parametrize("name", ["BIGINT", "HUGEINT", "UBIGINT", "DECIMAL(38,0)"])
def test_integral_numeric_schema(name):
    assert regrade.integral_type(name)


@pytest.mark.parametrize(
    "name", ["VARCHAR", "BOOLEAN", "DOUBLE", "FLOAT", "DECIMAL(38,2)"]
)
def test_nonintegral_or_text_schema_rejected(name):
    assert not regrade.integral_type(name)


def test_absent_usage_field_stays_unknown():
    assert regrade.reported_sum([1, 2]) == 3
    assert regrade.reported_sum([1, None]) is None
