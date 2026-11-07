"""Stateless conditional reads with bounded, exact delta or full-text replies."""

import hashlib
import re
from collections import Counter
from difflib import SequenceMatcher

from .delta import (
    MAX_DIFF_LINES,
    MAX_MATCH_PAIRS,
    MAX_SOURCE_BYTES,
    MAX_SOURCE_LINES,
    _metadata,
    _size,
)
from .store import Workbook, is_text

# A complete validation pass detects corruption and malformed UTF-8 even beyond
# the returned page. The cap bounds this work independently of caller budgets.
MAX_SYNC_SOURCE_BYTES = 16 * 1024 * 1024


def _read(workbook, row, *, missing_ok=False):
    try:
        with (workbook.blobs / row["digest"]).open("rb") as stream:
            raw = stream.read(MAX_SYNC_SOURCE_BYTES + 1)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise ValueError(
            "Sync source was deleted; resolve the current version"
        ) from None
    if (
        len(raw) != row["size_bytes"]
        or hashlib.sha256(raw).hexdigest() != row["digest"]
    ):
        raise ValueError("Sync source changed outside the workbook")
    try:
        return raw, raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise ValueError(f"{row['name']} is not valid UTF-8 text") from None


def _changes(before, after):
    before, after = before.splitlines(keepends=True), after.splitlines(keepends=True)
    if max(len(before), len(after)) > MAX_SOURCE_LINES:
        return None
    start = 0
    while start < min(len(before), len(after)) and before[start] == after[start]:
        start += 1
    left_end, right_end = len(before), len(after)
    while (
        left_end > start
        and right_end > start
        and before[left_end - 1] == after[right_end - 1]
    ):
        left_end -= 1
        right_end -= 1
    left, right = before[start:left_end], after[start:right_end]
    if max(len(left), len(right)) > MAX_DIFF_LINES:
        return None
    counts = Counter(right)
    if sum(counts[line] for line in left) > MAX_MATCH_PAIRS:
        return None
    return [
        {"start": start + i, "delete": j - i, "lines": right[k:l]}
        for tag, i, j, k, l in SequenceMatcher(
            None, left, right, autojunk=False
        ).get_opcodes()
        if tag != "equal"
    ]


def sync(
    workbook: Workbook,
    ref: str,
    base: str | int | None = None,
    offset: int = 0,
    max_bytes: int = 8192,
    *,
    fits=None,
    cost=None,
) -> dict:
    """Synchronize current content with an exact delta or a full-text page.

    Initial requests (offset=0) resolve the current version of ref's name, even
    if ref is an older immutable ID. Use inspect for historical initial reads.
    Continuations (offset>0) keep the exact returned immutable version pinned,
    so a concurrent update cannot mix source snapshots within one full read.

    A base is an immutable ID or positive exact version number whose *complete*
    text the caller actually retains. Version numbers resolve only within ref's
    artifact name; deleted versions are never reused. Missing bases fall back to
    text; existing IDs must have the same name. Unchanged replies omit the base
    echo; deltas retain the requested base so callers can validate patch inputs.
    Continue full pages using the returned artifact ID and next_offset, not a
    mutable name. All offsets count UTF-8 bytes; no newline conversion occurs.

    max_bytes always bounds serialized UTF-8 JSON. Optional fits/cost callbacks
    assess the final response representation, for example a host's token budget.
    Prefix search conservatively finds a fitting page; token costs need not be
    monotone, and this does not promise the longest possible fitting prefix.
    """
    if type(max_bytes) is not int or not 1 <= max_bytes <= 32768:
        raise ValueError("Sync max_bytes must be between 1 and 32768")
    if type(offset) is not int or offset < 0:
        raise ValueError("Sync offset must be a nonnegative integer")
    valid_version = type(base) is int and 1 <= base <= 2**63 - 1
    valid_id = isinstance(base, str) and re.fullmatch(r"wb_[0-9a-f]{32}", base)
    if base is not None and not (valid_version or valid_id):
        raise ValueError(
            "Sync base must be an immutable artifact ID or a positive same-name version up to 2^63-1"
        )
    with workbook.connect() as db:
        db.execute("BEGIN")
        current = workbook._get(db, ref)
        if offset == 0:
            current = workbook._get(db, current["name"])
        if valid_version:
            previous = db.execute(
                "SELECT * FROM artifacts WHERE name = ? AND version = ?",
                (current["name"], base),
            ).fetchone()
        elif base is not None:
            previous = db.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?", (base,)
            ).fetchone()
        else:
            previous = None
        if offset and current["artifact_id"] != ref:
            raise ValueError("Sync page continuation requires an immutable artifact ID")
        if previous is not None and current["name"] != previous["name"]:
            raise ValueError("Sync versions must have the same artifact name")
        if not is_text(current["media_type"]) or (
            previous is not None and not is_text(previous["media_type"])
        ):
            raise ValueError("Sync requires text artifacts")
        metadata = _metadata(workbook, db, current)
    if current["size_bytes"] > MAX_SYNC_SOURCE_BYTES:
        raise ValueError("Sync source exceeds the 16 MiB validation limit; use inspect")
    raw, text = _read(workbook, current)
    if offset > len(raw):
        raise ValueError("Sync offset exceeds the source size")
    if offset < len(raw) and raw[offset] & 0xC0 == 0x80:
        raise ValueError("Sync offset must be a UTF-8 character boundary")

    def accepted(value):
        return _size(value) <= max_bytes and (fits is None or fits(value))

    def score(value):
        return _size(value) if cost is None else cost(value)

    def page(body):
        end = offset + len(body.encode("utf-8"))
        complete = end == len(raw)
        return {
            "mode": "full",
            "artifact": metadata,
            "excerpt": body,
            "offset": offset,
            "next_offset": None if complete else end,
            "complete": complete,
        }

    candidates = []
    # No byte-valid JSON reply can contain more than max_bytes of source text.
    remaining = raw[offset : offset + max_bytes]
    # The whole source was already validated. Only the last codepoint may be
    # incomplete in this bounded prefix; decode strictly up to that boundary.
    end = len(remaining)
    while end and offset + end < len(raw) and raw[offset + end] & 0xC0 == 0x80:
        end -= 1
    prefix = remaining[:end].decode("utf-8")
    full = page(prefix)
    if full["complete"] and accepted(full):
        candidates.append(full)
    if offset == 0 and previous is not None:
        if previous["digest"] == current["digest"]:
            unchanged = {
                "mode": "unchanged",
                "artifact": metadata,
                "complete": True,
            }
            if accepted(unchanged):
                candidates.append(unchanged)
        elif max(previous["size_bytes"], current["size_bytes"]) <= MAX_SOURCE_BYTES:
            old = _read(workbook, previous, missing_ok=True)
            changes = None if old is None else _changes(old[1], text)
            if changes is not None:
                delta = {
                    "mode": "delta",
                    "artifact": metadata,
                    "base": base,
                    "changes": changes,
                    "complete": True,
                }
                if accepted(delta):
                    candidates.append(delta)
    if candidates:
        return min(candidates, key=score)
    if accepted(full):
        return full
    if not prefix or not accepted(page(prefix[:1])):
        raise ValueError("Sync budget cannot fit metadata and one source character")
    # Every remembered candidate was actually checked. Nonmonotone tokenizer
    # costs can leave unused budget but cannot cause an oversized response.
    best, low, high = page(prefix[:1]), 2, len(prefix) - 1
    while low <= high:
        middle = (low + high) // 2
        candidate = page(prefix[:middle])
        if accepted(candidate):
            best, low = candidate, middle + 1
        else:
            high = middle - 1
    return best
