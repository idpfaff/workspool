"""Supplementary exact-integer audit; never replaces the original model scores.

This post hoc correction accepts lossless integer-string previews only when an
immutable stored result has matching integral numeric types and exact values.
No provider calls, approvals, task execution, or workbook writes occur here.
"""

import argparse
import copy
import gzip
import hashlib
import importlib.util
import json
import re
import shutil
import sqlite3
import statistics
import zipfile
from collections import Counter
from decimal import Decimal
from pathlib import Path

import duckdb

SPEC = importlib.util.spec_from_file_location(
    "approval_original", Path(__file__).with_name("approval_model_eval.py")
)
original = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(original)
INTEGER_TYPES = {
    "TINYINT",
    "SMALLINT",
    "INTEGER",
    "BIGINT",
    "HUGEINT",
    "UTINYINT",
    "USMALLINT",
    "UINTEGER",
    "UBIGINT",
    "UHUGEINT",
}
COLUMNS = ["total_cents", "budget_cents", "remaining_cents"]


def exact_integer(value):
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r"-?(?:0|[1-9][0-9]*)", value):
        return int(value)
    raise ValueError("Not an exact integer encoding")


def integral_type(name):
    return name in INTEGER_TYPES or bool(
        re.fullmatch(r"DECIMAL\((?:[1-9]|[12][0-9]|3[0-8]),0\)", name)
    )


def reported_sum(values):
    return None if any(value is None for value in values) else sum(values)


def materialized_proof(workspace, phase):
    audit, expected = phase["task_audit"], phase["expected"]
    values = [expected[column] for column in COLUMNS]
    if any(type(value) is not int for value in values):
        raise ValueError("Oracle must contain exact integers")
    preview = audit["final_result"]
    if preview["columns"] != COLUMNS or preview["preview_truncated"] is not False:
        raise ValueError("Incomplete or differently shaped preview")
    if len(preview["rows"]) != 1 or len(preview["rows"][0]) != 3:
        raise ValueError("Expected exactly one complete result row")
    if [exact_integer(value) for value in preview["rows"][0]] != values:
        raise ValueError("Preview values differ from oracle")
    with sqlite3.connect(
        (workspace / "workbook.sqlite3").as_uri() + "?mode=ro", uri=True
    ) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT payload FROM task_runs WHERE run_id=?", (audit["run_id"],)
        ).fetchone()
        if row is None:
            raise ValueError("Missing immutable run ledger")
        run = json.loads(row["payload"])
        if run["run_id"] != audit["run_id"] or run["state"] != "complete":
            raise ValueError("Run ledger is not the completed audited run")
        artifact_id = run["steps"][-1]["artifact_id"]
        artifact = db.execute(
            "SELECT digest,size_bytes FROM artifacts WHERE artifact_id=?",
            (artifact_id,),
        ).fetchone()
    if artifact is None or artifact["size_bytes"] > 1024 * 1024:
        raise ValueError("Missing or unexpectedly large result artifact")
    path = workspace / "blobs" / artifact["digest"]
    body = path.read_bytes()
    if (
        len(body) != artifact["size_bytes"]
        or hashlib.sha256(body).hexdigest() != artifact["digest"]
    ):
        raise ValueError("Stored result digest mismatch")
    with duckdb.connect(
        config={
            "autoinstall_known_extensions": "false",
            "autoload_known_extensions": "false",
        }
    ) as db:
        schema = db.execute(
            "DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]
        ).fetchall()
        rows = db.execute("SELECT * FROM read_parquet(?)", [str(path)]).fetchmany(2)
    if [column[0] for column in schema] != COLUMNS or not all(
        integral_type(column[1]) for column in schema
    ):
        raise ValueError("Stored columns are not integral numeric types")
    if len(rows) != 1 or any(type(value) not in (int, Decimal) for value in rows[0]):
        raise ValueError("Stored row is not exact integral numeric data")
    if any(
        isinstance(value, Decimal)
        and (not value.is_finite() or value != value.to_integral_value())
        for value in rows[0]
    ):
        raise ValueError("Stored decimal is not an exact integer")
    if [int(value) for value in rows[0]] != values:
        raise ValueError("Stored values differ from oracle")
    return {
        "artifact_id": artifact_id,
        "sha256": artifact["digest"],
        "bytes": len(body),
        "columns": COLUMNS,
        "numeric_types": [column[1] for column in schema],
        "exact_integer_values": values,
        "preview_encodings": [type(value).__name__ for value in preview["rows"][0]],
    }


def regrade(root):
    source = root / "results.json"
    body = source.read_bytes()
    original_report = json.loads(body)
    corrected = copy.deepcopy(original_report["records"])
    outcomes, proof_count = [], 0
    for record in corrected:
        observations = []
        for phase in record["phases"]:
            audit = phase.get("task_audit", {})
            eligible = (
                phase.get("model_answer_correct") is True
                and phase["expected"].get("published_current") is True
                and phase["expected"]["state"] == "complete"
                and audit.get("state") == "complete"
                and all(
                    audit.get(key) is True
                    for key in (
                        "source_integrity",
                        "valid_gated_plan",
                        "pinned_current_sources",
                        "published_current",
                    )
                )
                and not phase.get("unretained_receipts")
            )
            observation = {
                "phase": phase["phase"],
                "original_pass": phase["success"],
                "original_failure": phase["failure"],
                "numeric_audit_applicable": eligible,
                "changed": False,
            }
            if eligible:
                try:
                    observation["materialized_proof"] = materialized_proof(
                        root / record["case"]["id"] / record["arm"], phase
                    )
                    proof_count += 1
                    if phase["failure"] == "task_audit_failed":
                        phase.update(success=True, enforcement_pass=True, failure=None)
                        observation["changed"] = True
                except (
                    ValueError,
                    KeyError,
                    OSError,
                    sqlite3.Error,
                    duckdb.Error,
                ) as error:
                    observation["audit_failure"] = type(error).__name__
            observation["supplementary_pass"] = phase["success"]
            observations.append(observation)
        previous_pass = record["success"]
        record["success"] = len(record["phases"]) == 2 and all(
            phase["success"] for phase in record["phases"]
        )
        record["enforcement_pass"] = len(record["phases"]) == 2 and all(
            phase["enforcement_pass"] for phase in record["phases"]
        )
        outcomes.append(
            {
                "case": record["case"],
                "arm": record["arm"],
                "original_pass": previous_pass,
                "supplementary_pass": record["success"],
                "phases": observations,
            }
        )
    summary = original.summarize(corrected)
    comparisons = (
        summary["comparisons"].values() if "comparisons" in summary else (summary,)
    )
    for comparison in comparisons:
        both_correct = comparison["both_correct_pairs"]
        if both_correct["n"] < 2:
            both_correct.pop("bootstrap_95_mean_difference", None)
            both_correct["uncertainty"] = (
                "Insufficient both-correct pairs for an informative interval"
            )
    return {
        "kind": "Explicitly post hoc supplementary exact-numeric audit; original report unchanged",
        "input_report_sha256": hashlib.sha256(body).hexdigest(),
        "audit_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "rule": "Only correct model answers and complete/current/published gated runs with intact current sources qualify. Canonical integer-string previews must match the original integer oracle AND an immutable hash-verified integral numeric Parquet result. Floats, bools, text columns, rounding, unrelated failures and missing evidence are never accepted.",
        "arms_examined": len(outcomes),
        "phases_examined": sum(len(row["phases"]) for row in outcomes),
        "materialized_results_verified": proof_count,
        "changed_phase_scores": sum(
            phase["changed"] for row in outcomes for phase in row["phases"]
        ),
        "outcomes": outcomes,
        "original_summary": original_report["summary"],
        "supplementary_summary": summary,
        "no_new_model_calls": True,
    }


def publication_audit(root, report):
    artifacts = publications = catalogs = 0
    publication_proofs, measurements = [], {}
    for record in report["records"]:
        workspace = root / record["case"]["id"] / record["arm"]
        with sqlite3.connect(
            (workspace / "workbook.sqlite3").as_uri() + "?mode=ro", uri=True
        ) as db:
            db.row_factory = sqlite3.Row
            assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            catalogs += 1
            for artifact in db.execute("SELECT digest,size_bytes FROM artifacts"):
                body = (workspace / "blobs" / artifact["digest"]).read_bytes()
                assert (
                    len(body) == artifact["size_bytes"]
                    and hashlib.sha256(body).hexdigest() == artifact["digest"]
                )
                artifacts += 1
            for event in db.execute(
                "SELECT event_id,details FROM coordination_events WHERE kind='task.published'"
            ):
                details = json.loads(event["details"])
                run = json.loads(
                    db.execute(
                        "SELECT payload FROM task_runs WHERE run_id=?",
                        (details["run_id"],),
                    ).fetchone()[0]
                )
                gate = run["steps"][-1]["approval_ref"]
                digest = db.execute(
                    "SELECT digest FROM artifacts WHERE artifact_id=?", (gate,)
                ).fetchone()[0]
                approval = db.execute(
                    "SELECT event_id,actor,kind FROM coordination_events WHERE resource=? AND event_id<? AND role='approver' AND kind IN ('approval.accepted','approval.revoked') AND json_extract(details,'$.sha256')=? ORDER BY event_id DESC LIMIT 1",
                    (gate, event["event_id"], digest),
                ).fetchone()
                assert approval is not None and approval["kind"] == "approval.accepted"
                assert record["case"]["scenario"] in {"approved", "handoff"}
                for source in run["sources"].values():
                    assert (
                        db.execute(
                            "SELECT artifact_id FROM latest WHERE name=?",
                            (source["name"],),
                        ).fetchone()[0]
                        == source["artifact_id"]
                    )
                publications += 1
                publication_proofs.append(
                    {
                        "case": record["case"]["id"],
                        "arm": record["arm"],
                        "run_id": run["run_id"],
                        "publication_event": event["event_id"],
                        "gate": gate,
                        "gate_sha256": digest,
                        "prior_approval_event": approval["event_id"],
                        "approver": approval["actor"],
                    }
                )
        for phase in record["phases"]:
            for metric in original.shared.METRICS:
                values = [
                    request["usage"][metric] for request in phase["provider_requests"]
                ]
                assert phase["usage"][metric] == reported_sum(values)
        for metric in original.shared.METRICS:
            assert record["usage"][metric] == reported_sum(
                [phase["usage"][metric] for phase in record["phases"]]
            )
    # Historical reports retain their measured arm names even when the current
    # evaluator adds or renames arms. Never infer their protocol from live code.
    arms = tuple(dict.fromkeys(row["arm"] for row in report["records"]))
    assert arms and set(arms) == set(report["summary"]["arms"])
    for arm in arms:
        records = [row for row in report["records"] if row["arm"] == arm]
        phases = [phase for row in records for phase in row["phases"]]
        calls = [call for phase in phases for call in phase["tool_calls"]]
        actions, help_calls = Counter(), Counter()
        for call in calls:
            arguments = call.get("arguments") or {}
            operation = (
                arguments.get("tool", "invalid")
                if call["name"] == "workspool"
                else call["name"]
            )
            target = (
                help_calls
                if call["name"] == "workspool" and "arguments" not in arguments
                else actions
            )
            target[operation] += 1
        measurements[arm] = {
            "provider_requests": sum(
                len(phase["provider_requests"]) for phase in phases
            ),
            "tool_calls": len(calls),
            "tool_errors": sum(bool(call["error"]) for call in calls),
            "action_selections": dict(actions),
            "schema_help_selections": dict(help_calls),
            "phase_failures": dict(
                Counter(phase["failure"] or "none" for phase in phases)
            ),
            "mean_arm_wall_seconds": statistics.mean(
                row["wall_seconds"] for row in records
            ),
            "median_arm_wall_seconds": statistics.median(
                row["wall_seconds"] for row in records
            ),
            "provider_seconds_sum": sum(
                request["seconds"]
                for phase in phases
                for request in phase["provider_requests"]
            ),
        }
        for metric in original.shared.METRICS:
            assert report["summary"]["arms"][arm]["usage"][metric] == reported_sum(
                [row["usage"][metric] for row in records]
            )
    requests = sum(value["provider_requests"] for value in measurements.values())
    assert requests == report["budget"]["requests_attempted"]
    assert (
        report["sources_unchanged"]
        and report["source_sha256"] == report["source_sha256_after"]
    )
    return {
        "passed": True,
        "catalogs_checked": catalogs,
        "artifact_hashes_and_sizes_checked": artifacts,
        "publications_checked": publications,
        "unauthorized_publications": 0,
        "publication_proofs": publication_proofs,
        "provider_requests_reconciled": requests,
        "usage_reconciled_at_request_phase_arm_levels": True,
        "measurements": measurements,
    }


def publish(root, destination):
    report_path = root / "results.json"
    report = json.loads(report_path.read_text())
    text = report_path.read_text()
    if re.search(
        r"/Users/|/private/|sk-(?:proj-)?[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16}|Bearer [A-Za-z0-9._-]{20,}",
        text,
    ):
        raise ValueError("Public report requires a separate privacy review")
    supplement = regrade(root)
    audit = publication_audit(root, report)
    destination.mkdir(parents=True, exist_ok=False)
    for name in ("results.json", "protocol.json"):
        shutil.copyfile(root / name, destination / name)
    (destination / "supplementary.json").write_text(
        json.dumps(supplement, indent=2) + "\n"
    )
    with gzip.open(destination / "traces.json.gz", "wt") as stream:
        json.dump(report["records"], stream, ensure_ascii=False)
    with zipfile.ZipFile(
        destination / "measured-source.zip", "w", compression=zipfile.ZIP_DEFLATED
    ) as archive:
        hashes = {}
        for relative, expected in report["source_sha256"].items():
            path = Path(relative)
            if path.is_absolute() or ".." in path.parts or path.suffix != ".py":
                raise ValueError("Unexpected measured source path")
            local = root / "source-snapshot" / path
            if not local.exists():
                local = Path(__file__).resolve().parents[1] / path
            body = local.read_bytes()
            if hashlib.sha256(body).hexdigest() != expected:
                raise ValueError("Missing original measured source: " + relative)
            archive.writestr(relative, body)
            hashes[relative] = expected
        supporting = {}
        for relative in (
            "LICENSE",
            "src/agent_workbook/_tokenizers/LICENSE.tiktoken",
            "pyproject.toml",
            "uv.lock",
        ):
            local = root / "source-snapshot" / relative
            if not local.exists():
                local = Path(__file__).resolve().parents[1] / relative
            body = local.read_bytes()
            archive.writestr(relative, body)
            supporting[relative] = hashlib.sha256(body).hexdigest()
        archive.writestr(
            "SHA256-MANIFEST.json",
            json.dumps(
                {"measured_python": hashes, "supporting_files": supporting}, indent=2
            )
            + "\n",
        )
        archive.writestr(
            "REPRODUCTION.txt",
            (
                "Workspool model evaluation source snapshot.\n"
                f"The {len(hashes)} Python files are the exact bytes identified by this run's source_sha256;\n"
                "supporting license/dependency files are listed separately in SHA256-MANIFEST.json.\n"
                "This is a partial source snapshot, not a complete distribution: obtain the\n"
                "matching Workspool project/dependency environment, including tokenizer resources,\n"
                "from https://github.com/idpfaff/workspool, then overlay these Python files in an\n"
                "isolated checkout. See protocol.json and results.json beside this archive for\n"
                "the exact model, dependency versions, cases and limits. The harness command is\n"
                "python benchmarks/approval_model_eval.py --env-file PRIVATE_KEY_FILE\n"
                "  --output NEW_PRIVATE_OUTPUT_DIRECTORY --concurrency 2\n"
                + (
                    "For this three-arm protocol also supply --baseline-wheel PATH_TO_PUBLISHED_V0.9.2_WHEEL\n"
                    "  --execute (omit --execute for offline preflight only).\n"
                    if "comparisons" in report["summary"]
                    else ""
                )
                + "A rerun makes paid API calls and requires authorized credentials. No credentials,\n"
                "environment files, workbook catalogs, blobs or source datasets are included.\n"
            ),
        )
    audit["source_files_archived"] = len(report["source_sha256"])
    audit["common_credential_patterns_found"] = False
    audit["absolute_personal_paths_found"] = False
    audit["files_sha256"] = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(destination.iterdir())
    }
    (destination / "audit.json").write_text(json.dumps(audit, indent=2) + "\n")
    return supplement, audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    output = parser.add_mutually_exclusive_group(required=True)
    output.add_argument("--output", type=Path)
    output.add_argument("--publish-dir", type=Path)
    args = parser.parse_args()
    root = args.run.expanduser().resolve()
    if args.publish_dir:
        result, _ = publish(root, args.publish_dir)
    else:
        result = regrade(root)
        with args.output.open("x") as stream:
            json.dump(result, stream, indent=2)
            stream.write("\n")
    print(
        json.dumps(
            {
                "arms_examined": result["arms_examined"],
                "changed_phase_scores": result["changed_phase_scores"],
                "summary": result["supplementary_summary"],
            }
        )
    )


if __name__ == "__main__":
    main()
