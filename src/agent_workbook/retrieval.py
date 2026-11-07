"""Bounded literal search over immutable source blobs, independent of the FTS prefix."""

import base64
import binascii
import codecs
import hashlib
import json
import re
import struct

from .store import Workbook, is_text

MAX_SCAN_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_BYTES = 32 * 1024
WINDOW_BYTES = 256 * 1024
CURSOR = struct.Struct(">B16s16sIQ")


def _digest(value):
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode()).digest()[:16]


def _scope(workbook, refs):
    if refs is not None and (
        not isinstance(refs, list)
        or not 1 <= len(refs) <= 16
        or any(not isinstance(ref, str) or not ref for ref in refs)
        or len(set(refs)) != len(refs)
    ):
        raise ValueError("Provide 1-16 distinct artifact references, or omit refs")
    selected = []
    with workbook.connect() as db:
        db.execute("BEGIN")
        rows = (
            [workbook._get(db, ref) for ref in refs]
            if refs is not None
            else db.execute("SELECT * FROM latest ORDER BY name").fetchall()
        )
        seen = set()
        for row in rows:
            if not is_text(row["media_type"]):
                if refs is not None:
                    raise ValueError(f"{row['name']} is not searchable text")
                continue
            if row["artifact_id"] in seen:
                raise ValueError("References must resolve to distinct versions")
            seen.add(row["artifact_id"])
            metadata = {
                key: row[key] for key in ("artifact_id", "name", "version")
            } | workbook._status(db, row["artifact_id"])
            selected.append((metadata, row["digest"], row["size_bytes"]))
    return selected, _digest({"refs": refs, "artifacts": selected})


def _cursor(snapshot, query, index, position):
    # This is a continuation, not an authorization token. Both fingerprints
    # bind it to the caller-selected catalog snapshot and literal query.
    return base64.urlsafe_b64encode(
        CURSOR.pack(1, snapshot, query, index, position)
    ).decode()


def _position(cursor, snapshot, query, selected):
    if cursor is None:
        return 0, 0
    try:
        if not isinstance(cursor, str) or len(cursor) != 60:
            raise ValueError
        version, saved_scope, saved_query, index, position = CURSOR.unpack(
            base64.b64decode(cursor, altchars=b"-_", validate=True)
        )
        if version != 1:
            raise ValueError
        if saved_scope != snapshot or saved_query != query:
            raise ValueError("Search snapshot or query changed; restart without cursor")
        if not 0 <= index < len(selected) or not 0 <= position <= selected[index][2]:
            raise ValueError
        return index, position
    except (binascii.Error, UnicodeError, struct.error, TypeError, ValueError) as error:
        if str(error).startswith("Search snapshot"):
            raise
        raise ValueError("Invalid search cursor") from None


def _floor_boundary(data, offset):
    while 0 < offset < len(data) and data[offset] & 0xC0 == 0x80:
        offset -= 1
    return offset


def _ceil_boundary(data, offset):
    while offset < len(data) and data[offset] & 0xC0 == 0x80:
        offset += 1
    return offset


def search(
    workbook: Workbook, text, refs=None, limit=5, context_bytes=256, cursor=None
):
    """Find case-insensitive literal occurrences, including overlaps.

    Context is at most context_bytes on each side, rounded inward to UTF-8
    boundaries. Cursor continuations use the same query and scope. A complete
    result means every selected source byte was searched; hit/output/scan limits
    always return a continuation. Binary references and malformed UTF-8 fail
    explicitly. No request depends on what another caller has already read.
    """
    if not isinstance(text, str) or not text.strip() or len(text) > 500:
        raise ValueError("Search text must contain 1-500 characters and not be blank")
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("Search limit must be between 1 and 20")
    if type(context_bytes) is not int or not 0 <= context_bytes <= 2048:
        raise ValueError("Search context_bytes must be between 0 and 2048")
    selected, snapshot = _scope(workbook, refs)
    query = _digest(text)
    index, position = _position(cursor, snapshot, query, selected)
    pattern = re.compile(re.escape(text), re.IGNORECASE)
    # A Unicode code point occupies at most four UTF-8 bytes. Lookahead covers
    # any match starting in this window, plus its requested right-hand context.
    lookahead = len(text) * 4 + context_bytes + 4
    hits, scanned, output_bytes = [], 0, 0

    def finish():
        nonlocal index, position
        while index < len(selected) and position == selected[index][2]:
            index, position = index + 1, 0
        complete = index == len(selected)
        return {
            "hits": hits,
            "cursor": None if complete else _cursor(snapshot, query, index, position),
            "complete": complete,
            "scanned_bytes": scanned,
        }

    while index < len(selected):
        metadata, digest, size = selected[index]
        if position == size:
            index, position = index + 1, 0
            continue
        start = max(0, position - context_bytes - 3)
        available = MAX_SCAN_BYTES - scanned
        core_size = min(WINDOW_BYTES, size - position)
        if size - start > available:
            core_size = min(core_size, available - (position - start) - lookahead)
        if core_size < min(4, size - position):
            if not scanned:
                raise ValueError(
                    "Search scan budget is too small for this query and context"
                )
            return finish()
        read_end = min(size, position + core_size + lookahead)
        try:
            with (workbook.blobs / digest).open("rb") as stream:
                stream.seek(start)
                data = stream.read(read_end - start)
        except FileNotFoundError:
            raise ValueError(
                "Search source was deleted; restart without cursor"
            ) from None
        if len(data) != read_end - start:
            raise ValueError("Search source changed outside the workbook")
        scanned += len(data)
        # The left context may begin inside a valid multi-byte character.
        leading = _ceil_boundary(data, 0) if start else 0
        start += leading
        data = data[leading:]
        try:
            decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
            decoded = decoder.decode(data, final=read_end == size)
            pending = len(decoder.getstate()[0])
            if pending:
                data = data[:-pending]
            local_position = position - start
            if (
                local_position < 0
                or _floor_boundary(data, local_position) != local_position
            ):
                raise ValueError(
                    "Invalid search cursor: offset is not a UTF-8 boundary"
                )
            char_position = len(data[:local_position].decode("utf-8"))
        except UnicodeError:
            raise ValueError(f"{metadata['name']} is not valid UTF-8 text") from None
        core_end = start + _floor_boundary(data, position + core_size - start)
        while match := pattern.search(decoded, char_position):
            match_start = len(decoded[: match.start()].encode())
            if start + match_start >= core_end:
                break
            match_end = len(decoded[: match.end()].encode())
            left = _ceil_boundary(data, max(0, match_start - context_bytes))
            right = _floor_boundary(data, min(len(data), match_end + context_bytes))
            hit = metadata | {
                "start_byte": start + left,
                "end_byte": start + right,
                "match_start_byte": start + match_start,
                "match_end_byte": start + match_end,
                "excerpt": data[left:right].decode("utf-8"),
            }
            hit_bytes = len(json.dumps(hit, ensure_ascii=False).encode())
            if output_bytes + hit_bytes > MAX_OUTPUT_BYTES:
                if not hits:
                    raise ValueError("Search output budget is too small for one hit")
                position = start + match_start
                return finish()
            hits.append(hit)
            output_bytes += hit_bytes
            # Advance one character, rather than the whole match, to retain
            # overlapping literal occurrences on this and the following page.
            char_position = match.start() + 1
            position = start + len(decoded[:char_position].encode())
            if len(hits) == limit:
                return finish()
        position = core_end
    return finish()
