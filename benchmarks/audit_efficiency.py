"""Independently recount efficiency.py traces without importing its measurement code.

python benchmarks/audit_efficiency.py .local-benchmarks/efficiency
"""

import argparse
import hashlib
import json
from pathlib import Path

import tiktoken


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--tokenizer", type=Path)
    args = parser.parse_args()
    result_bytes = (args.directory / "results.json").read_bytes()
    results = json.loads(result_bytes)
    trace_bytes = (args.directory / "traces.json").read_bytes()
    assert hashlib.sha256(trace_bytes).hexdigest() == results["trace_sha256"]
    assert not results.get("source_files_changed_during_run", [])
    encoders = {}
    for name in results["tokenizers"]:
        if name == "qwen2.5":
            from tokenizers import Tokenizer

            assert args.tokenizer, "Pass the measured Qwen tokenizer file"
            assert (
                hashlib.sha256(args.tokenizer.read_bytes()).hexdigest()
                == results["tokenizers"][name]["tokenizer_sha256"]
            )
            tokenizer = Tokenizer.from_file(str(args.tokenizer))
            encoders[name] = lambda text, enc=tokenizer: len(
                enc.encode(text, add_special_tokens=False).ids
            )
        else:
            encoding = tiktoken.get_encoding(name)
            encoders[name] = lambda text, enc=encoding: len(
                enc.encode(text, disallowed_special=())
            )
    traces = json.loads(trace_bytes)
    indexed = {trace["name"]: trace for trace in traces}
    assert len(indexed) == len(traces), "Duplicate trace names"
    for trace in traces:
        measured = trace["measured"]
        for codec, encode in encoders.items():
            parts = trace["parts"]
            categories = {
                kind: sum(map(encode, texts)) for kind, texts in parts.items()
            }
            assert {
                kind: values[codec] for kind, values in measured["categories"].items()
            } == categories
            assert measured["tokens"][codec] == sum(categories.values())
            base = sum(categories[kind] for kind in ("setup", "prompts", "requests"))
            assert measured["full_result_json_tokens"][codec] == base + sum(
                map(encode, trace["full_responses"])
            )
            if measured["structured_only_tokens"] is not None:
                assert measured["structured_only_tokens"][codec] == base + sum(
                    map(encode, trace["structured_responses"])
                )
    for case in results["cases"]:
        trace = indexed[case["case"] + "/" + case["profile"]]
        assert case["measured"] == trace["measured"]
        for codec, comparison in case["comparison"].items():
            assert comparison["workspool_tokens"] == trace["measured"]["tokens"][codec]
            expected = round(
                100 * (1 - comparison["workspool_tokens"] / comparison["raw_tokens"]), 2
            )
            assert comparison["reduction_pct"] == expected
    source_matches = {
        path: Path(path).exists()
        and hashlib.sha256(Path(path).read_bytes()).hexdigest() == sha
        for path, sha in results["source_sha256"].items()
    }
    audit = {
        "results_sha256": hashlib.sha256(result_bytes).hexdigest(),
        "trace_sha256": results["trace_sha256"],
        "audit_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "tokenizers": list(encoders),
        "traces_recounted": len(traces),
        "cases_verified": len(results["cases"]),
        "all_token_counts_match": True,
        "all_current_sources_match_recorded_hashes": all(source_matches.values()),
        "current_source_hash_matches": source_matches,
    }
    (args.directory / "audit.json").write_text(json.dumps(audit, indent=2) + "\n")
    print(json.dumps(audit, indent=2))


if __name__ == "__main__":
    main()
