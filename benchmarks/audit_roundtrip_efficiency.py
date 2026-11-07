"""Independently recount grouped MCP roundtrip traces and comparison arithmetic."""

import argparse
import hashlib
import json
from pathlib import Path


def main():
    import tiktoken

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    results_bytes = (args.directory / "results.json").read_bytes()
    report = json.loads(results_bytes)
    trace_bytes = (args.directory / "traces.json").read_bytes()
    assert hashlib.sha256(trace_bytes).hexdigest() == report["traces_sha256"]
    traces = json.loads(trace_bytes)
    indexed = {trace["name"]: trace for trace in traces}
    assert len(indexed) == len(traces)
    decisions, calls = 0, 0
    for trace in traces:
        events = trace["events"]
        prefixes = trace["decision_prefix_lengths"]
        measured = trace["measured"]
        assert prefixes == sorted(prefixes)
        assert all(0 <= end <= len(events) for end in prefixes)
        assert measured["tool_calls"] == len(trace["results"])
        assert measured["tool_calls"] == sum(
            event["role"] == "assistant_tool_call" for event in events
        )
        assert measured["scripted_decisions"] == len(prefixes)
        assert measured["max_reply_bytes"] == max(
            (
                len(event["text"].encode())
                for event in events
                if event["role"] == "tool"
            ),
            default=0,
        )
        decisions += len(prefixes)
        calls += measured["tool_calls"]
        for name in report["tokenizers"]:
            encoder = tiktoken.get_encoding(name)

            def count(text, encoding=encoder):
                return len(encoding.encode(text, disallowed_special=()))

            setup = sum(map(count, trace["setup"]))
            tokens = [count(event["text"]) for event in events]
            assert measured["setup_tokens"][name] == setup
            assert measured["instruction_tokens"][name] == sum(
                map(count, trace["setup"][:1])
            )
            assert measured["schema_tokens"][name] == sum(
                map(count, trace["setup"][1:])
            )
            assert measured["new_payload_tokens"][name] == setup + sum(tokens)
            assert measured["retained_input_exposure"][name] == sum(
                setup + sum(tokens[:end]) for end in prefixes
            )
            for metric, role in (
                ("tool_request_tokens", "assistant_tool_call"),
                ("tool_reply_tokens", "tool"),
                ("user_tokens", "user"),
                ("answer_tokens", "assistant_answer"),
            ):
                assert measured[metric][name] == sum(
                    token
                    for token, event in zip(tokens, events, strict=True)
                    if event["role"] == role
                )
    by_case = {}
    for case in report["cases"]:
        assert case["success"] and case["verification"]
        assert case["measured"] == indexed[case["case"] + "/" + case["arm"]]["measured"]
        by_case.setdefault(case["case"], {})[case["arm"]] = case
    for name, controls in report["comparisons_pct"].items():
        candidate = next(
            case for arm, case in by_case[name].items() if arm in {"sync", "retrieve"}
        )
        for arm, comparisons in controls.items():
            control = by_case[name][arm]
            for metric, codecs in comparisons.items():
                for codec, value in codecs.items():
                    assert value == round(
                        100
                        * (
                            1
                            - candidate["measured"][metric][codec]
                            / control["measured"][metric][codec]
                        ),
                        2,
                    )
        verifications = [case["verification"] for case in by_case[name].values()]
        for key in (
            "complete_source_sha256_by_turn",
            "requested_range_sha256",
            "source_sha256",
            "answers",
        ):
            present = [item[key] for item in verifications if key in item]
            assert not present or all(item == present[0] for item in present)
    matches = {
        path: Path(path).exists()
        and hashlib.sha256(Path(path).read_bytes()).hexdigest() == value
        for path, value in report["candidate_source_sha256"].items()
    }
    audit = {
        "results_sha256": hashlib.sha256(results_bytes).hexdigest(),
        "traces_sha256": report["traces_sha256"],
        "audit_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "traces_recounted": len(traces),
        "decision_prefixes_recounted": decisions,
        "tool_calls_recounted": calls,
        "tokenizers": report["tokenizers"],
        "all_payload_and_exposure_counts_match": True,
        "all_comparisons_and_verified_content_match": True,
        "all_current_candidate_sources_match": all(matches.values()),
        "current_candidate_source_matches": matches,
        "scope": "Independent recount of actual formatted payloads and grouped scripted prefixes, not provider billing or model-reliability validation.",
    }
    (args.directory / "audit.json").write_text(json.dumps(audit, indent=2) + "\n")
    print(json.dumps(audit, indent=2))


if __name__ == "__main__":
    main()
