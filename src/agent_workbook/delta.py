"""Bounded, exact line edits against an explicitly caller-held immutable version."""

import hashlib
import json
from collections import Counter
from difflib import SequenceMatcher

from .store import Workbook, is_text

MAX_SOURCE_BYTES = 1024 * 1024
MAX_SOURCE_LINES = 20000
MAX_DIFF_LINES = 2048
MAX_MATCH_PAIRS = 200000


def _size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def _metadata(workbook, db, row):
    item = workbook._summary(db, row) | {
        "sha256": row["digest"],
        "previous_id": row["previous_id"],
        "dependencies": workbook._dependencies(db, row["artifact_id"]),
    }
    if row["checkpoint"]:
        item["checkpoint"] = json.loads(row["checkpoint"])
    if row["recipe"]:
        item["has_recipe"] = True
    return item


def diff(workbook: Workbook, ref: str, base: str, max_bytes: int = 8192) -> dict:
    """Describe exact line splices, or explicitly require a full read.

    ``base`` must be an immutable artifact ID whose complete decoded text the
    caller retains. Apply changes in reverse order to
    ``base_text.splitlines(keepends=True)`` and join the lines without separators.
    No newline normalization or implicit session cache is used.

    The byte budget covers a complete successful JSON result before the MCP
    metadata projection. Fixed fallback metadata is not covered by that budget.
    Source and conservative matching limits prevent unbounded quadratic work.
    """
    if not isinstance(base, str) or not base.startswith("wb_"):
        raise ValueError("Diff base must be an immutable artifact ID")
    if type(max_bytes) is not int or not 1 <= max_bytes <= 32768:
        raise ValueError("Diff max_bytes must be between 1 and 32768")
    with workbook.connect() as db:
        db.execute("BEGIN")
        current = workbook._get(db, ref)
        previous = workbook._get(db, base)
        if previous["artifact_id"] != base:
            raise ValueError("Diff base must be an immutable artifact ID")
        if current["name"] != previous["name"]:
            raise ValueError("Diff versions must have the same artifact name")
        if not is_text(current["media_type"]) or not is_text(previous["media_type"]):
            raise ValueError("Diff requires two text artifacts")
        result = {"artifact": _metadata(workbook, db, current), "base": base}

    def fallback(reason):
        return result | {"requires_read": True, "reason": reason}

    if max(current["size_bytes"], previous["size_bytes"]) > MAX_SOURCE_BYTES:
        return fallback("source_too_large")
    sources = {}
    for row in (previous, current):
        digest = row["digest"]
        if digest in sources:
            continue
        try:
            with (workbook.blobs / digest).open("rb") as stream:
                raw = stream.read(MAX_SOURCE_BYTES + 1)
        except FileNotFoundError:
            raise ValueError(
                "Diff source was deleted; inspect the current version"
            ) from None
        if len(raw) != row["size_bytes"] or hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("Diff source changed outside the workbook")
        try:
            sources[digest] = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise ValueError(f"{row['name']} is not valid UTF-8 text") from None
    before = sources[previous["digest"]].splitlines(keepends=True)
    after = sources[current["digest"]].splitlines(keepends=True)
    if max(len(before), len(after)) > MAX_SOURCE_LINES:
        return fallback("too_many_lines")

    # Trim unchanged boundaries before matching. A one-line edit to a large,
    # repetitive document should not pay for matching all unchanged lines.
    start = 0
    while start < min(len(before), len(after)) and before[start] == after[start]:
        start += 1
    end_before, end_after = len(before), len(after)
    while (
        end_before > start
        and end_after > start
        and before[end_before - 1] == after[end_after - 1]
    ):
        end_before -= 1
        end_after -= 1
    left, right = before[start:end_before], after[start:end_after]
    if max(len(left), len(right)) > MAX_DIFF_LINES:
        return fallback("diff_too_complex")
    counts = Counter(right)
    if sum(counts[line] for line in left) > MAX_MATCH_PAIRS:
        return fallback("diff_too_complex")
    changes = [
        {"start": start + i, "delete": j - i, "lines": right[k:l]}
        for tag, i, j, k, l in SequenceMatcher(
            None, left, right, autojunk=False
        ).get_opcodes()
        if tag != "equal"
    ]
    delta = result | {"changes": changes}
    if _size(delta) > max_bytes:
        return fallback("delta_too_large")
    # There is no benefit in replacing a shorter complete body with edit
    # instructions. The unchanged case still verifies identity and freshness.
    full_read = result["artifact"] | {
        "excerpt": sources[current["digest"]],
        "next_offset": None,
    }
    if changes and _size(delta) >= _size(full_read):
        return fallback("full_read_smaller")
    return delta
