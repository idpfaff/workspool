"""The supplementary audit accepts exact encodings, never rounded or textual data."""

import hashlib
import importlib.util
import json
import zipfile
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


@pytest.mark.parametrize("arms", [("control", "candidate"), regrade.original.ARMS])
def test_publication_audit_uses_recorded_arms_not_current_evaluator(tmp_path, arms):
    from agent_workbook.store import Workbook

    records = []
    for arm in arms:
        Workbook(tmp_path / "approved-8201" / arm)
        records.append(
            {
                "case": {"id": "approved-8201", "scenario": "approved"},
                "arm": arm,
                "phases": [],
                "wall_seconds": 1.0,
                "usage": dict.fromkeys(regrade.original.shared.METRICS, 0),
                "attempted": False,
                "success": False,
                "enforcement_pass": False,
            }
        )
    report = {
        "records": records,
        "summary": regrade.original.summarize(records),
        "budget": {"requests_attempted": 0},
        "sources_unchanged": True,
        "source_sha256": {},
        "source_sha256_after": {},
    }
    result = regrade.publication_audit(tmp_path, report)
    assert result["passed"] and result["catalogs_checked"] == len(arms)
    assert set(result["measurements"]) == set(arms)
    assert result["provider_requests_reconciled"] == 0


@pytest.mark.parametrize("arms", [("control", "candidate"), regrade.original.ARMS])
def test_actual_regrade_and_publish_preserve_two_and_three_arm_reports(tmp_path, arms):
    """A failed/unattempted fixture must remain publishable, never become a win."""
    from agent_workbook.store import Workbook

    root = tmp_path / "private-run"
    records = []
    for arm in arms:
        Workbook(root / "approved-8201" / arm)
        records.append(
            {
                "case": {"id": "approved-8201", "scenario": "approved"},
                "arm": arm,
                "phases": [],
                "wall_seconds": 0.01,
                "usage": dict.fromkeys(regrade.original.shared.METRICS, 0),
                "attempted": False,
                "success": False,
                "enforcement_pass": False,
            }
        )
    relative = "benchmarks/approval_model_regrade.py"
    source = Path(regrade.__file__).read_bytes()
    snapshot = root / "source-snapshot" / relative
    snapshot.parent.mkdir(parents=True)
    snapshot.write_bytes(source)
    hashes = {relative: hashlib.sha256(source).hexdigest()}
    report = {
        "records": records,
        "summary": regrade.original.summarize(records),
        "budget": {"requests_attempted": 0},
        "sources_unchanged": True,
        "source_sha256": hashes,
        "source_sha256_after": hashes,
    }
    original_bytes = (json.dumps(report, indent=2) + "\n").encode()
    (root / "results.json").write_bytes(original_bytes)
    (root / "protocol.json").write_text(json.dumps({"arms": list(arms)}))

    supplement = regrade.regrade(root)
    summary = supplement["supplementary_summary"]
    assert supplement["changed_phase_scores"] == 0
    assert supplement["arms_examined"] == len(arms)
    assert set(summary["arms"]) == set(arms)
    comparisons = summary.get("comparisons", {"historical": summary})
    assert len(comparisons) == len(arms) - 1
    for comparison in comparisons.values():
        assert comparison["both_correct_pairs"]["n"] == 0
        assert "bootstrap_95_mean_difference" not in comparison["both_correct_pairs"]
        assert "Insufficient" in comparison["both_correct_pairs"]["uncertainty"]

    destination = tmp_path / "published-fixture"
    published_supplement, audit = regrade.publish(root, destination)
    assert published_supplement == supplement
    assert audit["passed"] and audit["catalogs_checked"] == len(arms)
    assert set(audit["measurements"]) == set(arms)
    assert (root / "results.json").read_bytes() == original_bytes
    assert (destination / "results.json").read_bytes() == original_bytes
    assert json.loads((destination / "supplementary.json").read_text()) == supplement
    with zipfile.ZipFile(destination / "measured-source.zip") as archive:
        assert archive.read(relative) == source
        reproduction = archive.read("REPRODUCTION.txt").decode()
        assert "The 1 Python files" in reproduction
        assert ("--baseline-wheel" in reproduction) == (len(arms) == 3)
