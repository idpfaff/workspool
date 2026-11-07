"""Retrieve literal matches and exact reads with shared metadata and source spans.

All references and freshness flags are resolved in one catalog snapshot before
blob IO. Overlapping or adjacent spans of the same immutable artifact are merged;
query and read arrays retain input order and refer to those spans by index.
Source reads, including repeated windows, share a 16 MiB budget. Query cursors
are compatible with search(); a query not reached within that budget returns its
starting continuation. Output budgets shorten reads first, then defer whole
search passages with continuations. No client/session knowledge is retained.
"""

import codecs
import json

from . import retrieval
from .store import Workbook, is_text

MAX_SCAN_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_BYTES = 32 * 1024


def _requests(queries, reads, max_bytes, fits):
    queries = [] if queries is None else queries
    reads = [] if reads is None else reads
    if (
        not isinstance(queries, list)
        or len(queries) > 8
        or not isinstance(reads, list)
        or len(reads) > 16
        or not (queries or reads)
    ):
        raise ValueError(
            "Provide up to 8 queries and 16 reads, with at least one request"
        )
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_OUTPUT_BYTES:
        raise ValueError(f"max_bytes must be between 1 and {MAX_OUTPUT_BYTES}")
    if fits is not None and not callable(fits):
        raise ValueError("fits must be a callable output-budget check")
    normalized_queries, normalized_reads = [], []
    for query in queries:
        if not isinstance(query, dict) or set(query) - {
            "text",
            "refs",
            "limit",
            "context_bytes",
            "cursor",
        }:
            raise ValueError(
                "Each query must contain text and supported search options"
            )
        item = {"refs": None, "limit": 5, "context_bytes": 256, "cursor": None} | query
        retrieval._validate_search(
            item.get("text"), item["limit"], item["context_bytes"]
        )
        normalized_queries.append(item)
    for read in reads:
        if not isinstance(read, dict) or set(read) - {"ref", "offset", "bytes"}:
            raise ValueError("Each read must contain ref and optional offset/bytes")
        item = {"offset": 0, "bytes": 1024} | read
        if not isinstance(item.get("ref"), str) or not item["ref"]:
            raise ValueError("Each read must name an artifact reference")
        if type(item["offset"]) is not int or item["offset"] < 0:
            raise ValueError("Read offset must be a nonnegative integer")
        if type(item["bytes"]) is not int or not 1 <= item["bytes"] <= MAX_OUTPUT_BYTES:
            raise ValueError(f"Read bytes must be between 1 and {MAX_OUTPUT_BYTES}")
        normalized_reads.append(item)
    return normalized_queries, normalized_reads


def _resolve(workbook, queries, reads):
    records, scopes, resolved_reads = {}, [], []
    with workbook.connect() as db:
        db.execute("BEGIN")

        def remember(artifact_id):
            if artifact_id not in records:
                row = workbook._get(db, artifact_id)
                metadata = {
                    key: row[key]
                    for key in (
                        "artifact_id",
                        "name",
                        "version",
                        "size_bytes",
                        "media_type",
                    )
                } | workbook._status(db, artifact_id)
                records[artifact_id] = (metadata, row["digest"])

        for query in queries:
            selected, snapshot = retrieval._scope(workbook, query["refs"], db=db)
            retrieval._position(
                query["cursor"], snapshot, retrieval._digest(query["text"]), selected
            )
            scopes.append((selected, snapshot))
            for metadata, _, _ in selected:
                remember(metadata["artifact_id"])
        for read in reads:
            row = workbook._get(db, read["ref"])
            if not is_text(row["media_type"]):
                raise ValueError(f"{row['name']} is not readable UTF-8 text")
            if read["offset"] > row["size_bytes"]:
                raise ValueError("Read offset is beyond the end of the artifact")
            remember(row["artifact_id"])
            resolved_reads.append(row["artifact_id"])
    return records, scopes, resolved_reads


def _read(workbook, record, offset, count):
    metadata, digest = record
    count = min(count, metadata["size_bytes"] - offset)
    try:
        with (workbook.blobs / digest).open("rb") as stream:
            stream.seek(offset)
            data = stream.read(count)
    except FileNotFoundError:
        raise ValueError("Retrieval source was deleted; restart the request") from None
    if len(data) != count:
        raise ValueError("Retrieval source changed outside the workbook")
    try:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        text = decoder.decode(data, final=offset + count == metadata["size_bytes"])
        pending = len(decoder.getstate()[0])
    except UnicodeError:
        raise ValueError(
            f"{metadata['name']} is not valid UTF-8 at this read; offset must be a UTF-8 boundary"
        ) from None
    end = offset + count - pending
    if end == offset and offset < metadata["size_bytes"]:
        raise ValueError("Read bytes budget is too small for the next UTF-8 character")
    return text, end, count


def _merge(spans):
    """Merge source bytes, checking overlapping copies agree before deduplicating."""
    merged = []
    locations = {}
    for original, span in sorted(
        enumerate(spans),
        key=lambda pair: (
            pair[1]["artifact"],
            pair[1]["start_byte"],
            pair[1]["end_byte"],
        ),
    ):
        data = span["excerpt"].encode()
        if (
            merged
            and merged[-1]["artifact"] == span["artifact"]
            and span["start_byte"] <= merged[-1]["end_byte"]
        ):
            previous = merged[-1]
            offset = span["start_byte"] - previous["start_byte"]
            overlap = min(len(data), previous["end_byte"] - span["start_byte"])
            previous_data = previous["excerpt"].encode()
            if previous_data[offset : offset + overlap] != data[:overlap]:
                raise ValueError("Retrieval source changed outside the workbook")
            if span["end_byte"] > previous["end_byte"]:
                previous["excerpt"] = (previous_data + data[overlap:]).decode()
                previous["end_byte"] = span["end_byte"]
        else:
            merged.append(span.copy())
        locations[original] = len(merged) - 1
    return merged, locations


def _assemble(records, queries, scopes, found, loaded, kept, read_chars, scanned):
    """Rebuild all references after selecting prefixes; never reread source data."""
    artifacts, indices, spans, reads, query_results = [], {}, [], [], []

    def add_span(artifact_id, start, end, excerpt):
        if artifact_id not in indices:
            indices[artifact_id] = len(artifacts)
            artifacts.append(records[artifact_id][0])
        spans.append(
            {
                "artifact": indices[artifact_id],
                "start_byte": start,
                "end_byte": end,
                "excerpt": excerpt,
            }
        )
        return len(spans) - 1

    for artifact_id, start, body in loaded:
        excerpt = body[:read_chars]
        read_chars -= len(excerpt)
        end = start + len(excerpt.encode())
        reads.append(
            {
                "span": add_span(artifact_id, start, end, excerpt),
                "start_byte": start,
                "end_byte": end,
                "next_offset": end
                if end < records[artifact_id][0]["size_bytes"]
                else None,
            }
        )
    for query, (selected, snapshot), result, count in zip(queries, scopes, found, kept):
        matches = [
            {
                "span": add_span(
                    hit["artifact_id"],
                    hit["start_byte"],
                    hit["end_byte"],
                    hit["excerpt"],
                ),
                "start_byte": hit["match_start_byte"],
                "end_byte": hit["match_end_byte"],
            }
            for hit in result["hits"][:count]
        ]
        cursor, complete = result["cursor"], result["complete"]
        if count < len(result["hits"]):
            omitted = result["hits"][count]
            index = next(
                i
                for i, (metadata, _, _) in enumerate(selected)
                if metadata["artifact_id"] == omitted["artifact_id"]
            )
            cursor = retrieval._cursor(
                snapshot,
                retrieval._digest(query["text"]),
                index,
                omitted["match_start_byte"],
            )
            complete = False
        query_results.append(
            {"matches": matches, "complete": complete, "cursor": cursor}
        )
    spans, locations = _merge(spans)
    for entry in reads + [
        match for query in query_results for match in query["matches"]
    ]:
        entry["span"] = locations[entry["span"]]
    return {
        "artifacts": artifacts,
        "spans": spans,
        "queries": query_results,
        "reads": reads,
        "scanned_bytes": scanned,
    }


def _progress(result, queries, scopes):
    if all(q["complete"] for q in result["queries"]) and all(
        r["next_offset"] is None for r in result["reads"]
    ):
        return True
    if any(r["end_byte"] > r["start_byte"] for r in result["reads"]):
        return True
    for request, (selected, snapshot), response in zip(
        queries, scopes, result["queries"]
    ):
        query = retrieval._digest(request["text"])
        before = retrieval._position(request["cursor"], snapshot, query, selected)
        after = (
            (len(selected), 0)
            if response["complete"]
            else retrieval._position(response["cursor"], snapshot, query, selected)
        )
        if response["matches"] or after > before:
            return True
    return False


def _fit_output(build, kept, read_chars, accepted):
    """Bounded conservative search; every returned candidate passed accepted()."""
    full = build(kept, read_chars)
    if accepted(full):
        return full
    # Keep complete query passages ahead of read bodies. A pending read remains
    # in its original slot, with an empty exact span and unchanged next_offset.
    while True:
        best = build(kept, 0)
        if accepted(best):
            break
        # Token costs need not be monotone: even one character can tokenize
        # more cheaply than an empty string in the surrounding representation.
        if read_chars:
            one = build(kept, 1)
            if accepted(one):
                best = one
                break
        index = next((i for i in reversed(range(len(kept))) if kept[i]), None)
        if index is None:
            raise ValueError(
                "Retrieval output budget cannot fit required metadata; increase the budget or reduce the batch"
            )
        kept[index] -= 1
    if read_chars:
        full = build(kept, read_chars)
        if accepted(full):
            return full
        low, high = 1, read_chars - 1
        while low <= high:
            middle = (low + high) // 2
            candidate = build(kept, middle)
            if accepted(candidate):
                best, low = candidate, middle + 1
            else:
                high = middle - 1
    return best


def retrieve(
    workbook: Workbook, queries=None, reads=None, max_bytes=32768, *, fits=None
):
    """Return budgeted exact spans with explicit query and read continuations.

    Queries accept search() options; reads accept ref, offset (default 0), and
    bytes (default 1024, maximum 32768). Read boundaries are exact UTF-8 byte
    offsets. Continue reads using the returned immutable artifact ID.

    max_bytes caps complete compact JSON (1..32768); fits additionally checks
    the actual formatted response. Oversized batches shorten read prefixes
    first, then defer whole query passages. Every request retains its array
    slot. A pending read has an empty span and next_offset equal to its input
    offset; omitted matches reset the query cursor to the first omitted match.
    Continue only unfinished requests. If metadata or any logical progress
    cannot fit, raise an explicit budget error. Candidate search is bounded
    and does not assume token cost is monotone or promise maximal packing.
    """
    queries, reads = _requests(queries, reads, max_bytes, fits)
    records, scopes, resolved_reads = _resolve(workbook, queries, reads)
    loaded, found, scanned = [], [], 0
    for read, artifact_id in zip(reads, resolved_reads):
        size = records[artifact_id][0]["size_bytes"]
        needed = min(read["bytes"], size - read["offset"])
        if needed > MAX_SCAN_BYTES - scanned:
            raise ValueError(
                "Retrieval scan budget is too small for the requested reads"
            )
        body, _, consumed = _read(
            workbook, records[artifact_id], read["offset"], read["bytes"]
        )
        scanned += consumed
        loaded.append((artifact_id, read["offset"], body))
    for query, (selected, snapshot) in zip(queries, scopes):
        result = retrieval._search_selected(
            workbook,
            query["text"],
            selected,
            snapshot,
            query["limit"],
            query["context_bytes"],
            query["cursor"],
            scan_budget=MAX_SCAN_BYTES - scanned,
            allow_pending=True,
        )
        scanned += result["scanned_bytes"]
        found.append(result)

    def build(kept, read_chars):
        return _assemble(
            records, queries, scopes, found, loaded, kept, read_chars, scanned
        )

    def accepted(result):
        return len(
            json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
        ) <= max_bytes and (fits is None or fits(result))

    result = _fit_output(
        build,
        [len(q["hits"]) for q in found],
        sum(len(body) for _, _, body in loaded),
        accepted,
    )
    if not _progress(result, queries, scopes):
        raise ValueError(
            "Retrieval output budget cannot fit progress; increase the budget or reduce query context/batch size"
        )
    return result
