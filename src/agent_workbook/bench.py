"""Measure the context payload of two handoff strategies on a fixed dataset."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from .query import query
from .store import Workbook


def benchmark(tokenizer: Path | None = None) -> dict:
    values = [(i, (i % 21) - 10) for i in range(2000)]
    csv = "bar_id,return_bp\n" + "".join(f"{i},{bp}\n" for i, bp in values)
    question = (
        "How many of the 2,000 bars had a positive return, and what was the sum in bp?"
    )
    summary = "Synthetic replay of 2,000 bars with integer returns in basis points."
    with TemporaryDirectory() as folder:
        root = Path(folder)
        workbook = Workbook(root)
        dataset = workbook.save_text(
            csv,
            name="replay/bars",
            kind="table",
            media_type="text/csv",
            summary=summary,
        )
        workbook.save_text(
            "",
            name="replay/handoff",
            kind="checkpoint",
            media_type="text/plain",
            checkpoint={
                "objective": "Summarize the synthetic replay.",
                "verified": "Fixture contains 2,000 generated bars.",
                "next_step": question,
            },
            dependencies=[dataset["artifact_id"]],
        )
        reopened = Workbook(root)
        resume = reopened.resume("replay")
        result = query(
            reopened,
            resume["checkpoint"]["dependencies"],
            """
            SELECT count(*) FILTER (WHERE return_bp > 0) AS positive_bars,
                   CAST(sum(return_bp) AS BIGINT) AS total_bp
            FROM input1
            """,
        )
        expected = [sum(bp > 0 for _, bp in values), sum(bp for _, bp in values)]
        if result["rows"] != [expected]:
            raise RuntimeError("Workbook and independent calculation disagreed")

    raw_prompt = question + "\n" + summary + "\n" + csv
    ref_prompt = (
        question
        + "\n"
        + json.dumps(
            {"resume": resume, "query": result},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    raw_bytes = len(raw_prompt.encode("utf-8"))
    ref_bytes = len(ref_prompt.encode("utf-8"))
    report = {
        "workload": "Synthetic 2,000-row replay. Raw rows in context vs saved workbook and exact aggregates.",
        "scope": "Payload only. Excludes MCP schemas, call arguments, model output, and inference time.",
        "expected_result": dict(zip(result["columns"], expected, strict=True)),
        "raw_handoff_bytes": raw_bytes,
        "workbook_handoff_bytes": ref_bytes,
        "byte_reduction_pct": round((1 - ref_bytes / raw_bytes) * 100, 1),
    }
    if tokenizer:
        from tokenizers import Tokenizer

        codec = Tokenizer.from_file(str(tokenizer))
        raw_tokens, ref_tokens = [
            len(codec.encode(text, add_special_tokens=False).ids)
            for text in (raw_prompt, ref_prompt)
        ]
        report |= {
            "raw_handoff_tokens": raw_tokens,
            "workbook_handoff_tokens": ref_tokens,
            "token_reduction_pct": round((1 - ref_tokens / raw_tokens) * 100, 1)
            if raw_tokens
            else None,
        }
    return report
