"""Recount workflow traces independently, including accumulated decision prefixes."""

import argparse
import hashlib
import json
from pathlib import Path

import tiktoken


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    result_bytes = (args.directory / "results.json").read_bytes()
    results = json.loads(result_bytes)
    trace_bytes = (args.directory / "traces.json").read_bytes()
    assert hashlib.sha256(trace_bytes).hexdigest() == results["traces_sha256"]
    traces = json.loads(trace_bytes)
    lookup = {trace["name"]: trace for trace in traces}
    assert len(lookup) == len(traces)
    decisions = 0
    for trace in traces:
        measured = trace["measured"]
        prefixes = trace["decision_prefix_lengths"]
        events = trace["events"]
        assert prefixes == sorted(prefixes)
        assert all(0 <= end <= len(events) for end in prefixes)
        assert measured["model_decisions"] == len(prefixes)
        assert measured["tool_calls"] == sum(
            event["role"] == "assistant_tool_call" for event in events
        )
        assert len(trace["wire_results"]) == measured["tool_calls"]
        decisions += len(prefixes)
        for codec in results["tokenizers"]:
            encoder = tiktoken.get_encoding(codec)

            def count(text, encoding=encoder):
                return len(encoding.encode(text, disallowed_special=()))

            setup = sum(map(count, trace["setup"]))
            tokens = [count(event["text"]) for event in events]
            assert measured["setup_tokens"][codec] == setup
            assert measured["new_payload_tokens"][codec] == setup + sum(tokens)
            assert measured["final_context_tokens"][codec] == setup + sum(tokens)
            assert measured["cumulative_input_exposure"][codec] == sum(
                setup + sum(tokens[:end]) for end in prefixes
            )
            for field, role in (
                ("tool_reply_tokens", "tool"),
                ("assistant_tool_call_tokens", "assistant_tool_call"),
            ):
                assert measured[field][codec] == sum(
                    token
                    for token, event in zip(tokens, events, strict=True)
                    if event["role"] == role
                )
    cases = {case["case"]: case for case in results["cases"]}
    for name, case in cases.items():
        assert case["measured"] == lookup[name]["measured"]
        assert case["success"] == (case["answers"] == case["expected"])
    for name, comparison in results["comparisons"].items():
        baseline, candidate = cases[name + "/baseline"], cases[name + "/candidate"]
        assert comparison["baseline_success"] == baseline["success"]
        assert comparison["candidate_success"] == candidate["success"]
        if not baseline["success"] or not candidate["success"]:
            assert comparison["savings_pct"] is None
        else:
            for metric, values in comparison["savings_pct"].items():
                for codec, value in values.items():
                    assert value == round(
                        100
                        * (
                            1
                            - candidate["measured"][metric][codec]
                            / baseline["measured"][metric][codec]
                        ),
                        2,
                    )
    matches = {
        path: Path(path).exists()
        and hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
        for path, digest in results["candidate_source_sha256"].items()
    }
    audit = {
        "results_sha256": hashlib.sha256(result_bytes).hexdigest(),
        "traces_sha256": results["traces_sha256"],
        "audit_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "traces_recounted": len(traces),
        "decision_prefixes_recounted": decisions,
        "tokenizers": list(results["tokenizers"]),
        "all_trace_counts_match": True,
        "all_outcomes_and_comparisons_match": True,
        "all_current_candidate_sources_match": all(matches.values()),
        "current_candidate_source_matches": matches,
        "scope": "Independent token recount and comparison arithmetic; no inference, billing, or independent model-reliability validation.",
    }
    (args.directory / "audit.json").write_text(json.dumps(audit, indent=2) + "\n")
    print(json.dumps(audit, indent=2))


if __name__ == "__main__":
    main()
