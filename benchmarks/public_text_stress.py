"""Local real-corpus stress for indexed lexical retrieval and durable context.

No downloads or model calls. Inputs are exact UTF-8 text/wikitext files, not
rendered prose. Reports stay local until separately sanitized for publication.
"""

import argparse
import codecs
import hashlib
import importlib.util
import json
import re
import resource
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

from agent_workbook.context_index import ranked_search
from agent_workbook.retrieval import search
from agent_workbook.store import Workbook
from agent_workbook.tasks import define_task, run_task, status

MIB = 1024**2
CHUNK = 2048
OVERLAP = 256


def helpers():
    spec = importlib.util.spec_from_file_location(
        "text_stress_helpers", Path(__file__).with_name("task_stress.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def peak_rss():
    """Cumulative process high-water RSS, not a per-call increment or tree sum."""
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def scan_source(path):
    digest, size = hashlib.sha256(), 0
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(MIB), b""):
            digest.update(block)
            size += len(block)
            decoder.decode(block)
        decoder.decode(b"", final=True)
    return size, digest.hexdigest()


def corpus(documents, manifest=None, minimum_bytes=100 * MIB, maximum_bytes=1024 * MIB):
    root = Path(documents).resolve()
    manifest = Path(manifest) if manifest else root / "manifest.json"
    metadata = json.loads(manifest.read_text()) if manifest.exists() else {}
    entries = metadata.get("documents")
    if entries is None:
        entries = [
            {"path": str(path.relative_to(root))}
            for path in sorted(root.rglob("*.txt"))
        ]
    if not isinstance(entries, list) or not 2 <= len(entries) <= 10000:
        raise ValueError(
            "Provide 2-10000 real text documents; select an explicit subset before import"
        )
    records, seen, total = [], set(), 0
    for position, item in enumerate(entries):
        relative = item["path"]
        path = root / relative
        if (
            Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or path.is_symlink()
            or not path.is_file()
            or not path.resolve().is_relative_to(root)
        ):
            raise ValueError(
                "Corpus paths must name regular files inside the source directory"
            )
        if path.resolve() in seen:
            raise ValueError("Duplicate corpus document")
        seen.add(path.resolve())
        total += path.stat().st_size
        if total > maximum_bytes:
            raise ValueError("Corpus exceeds the configured input-byte cap")
        size, digest = scan_source(path)
        if not size:
            raise ValueError(
                "Empty documents must be excluded explicitly in the acquisition manifest"
            )
        if item.get("bytes", size) != size or item.get("sha256", digest) != digest:
            raise ValueError("Corpus manifest size/hash mismatch")
        records.append(
            {
                "name": f"public/{position:05d}",
                "path": str(path.resolve()),
                "relative_path": relative,
                "bytes": size,
                "sha256": digest,
                "title": str(item.get("title", path.stem)),
                "source_url": item.get("source_url", item.get("url", "")),
                **{key: item[key] for key in ("page_id", "revision_id") if key in item},
            }
        )
    if total < minimum_bytes:
        raise ValueError("Corpus is below the required real-source byte minimum")
    return records, {
        key: value for key, value in metadata.items() if key != "documents"
    }


def boundary(raw, end):
    while 0 < end < len(raw) and raw[end] & 0xC0 == 0x80:
        end -= 1
    return end


def derive_queries(records, count=6):
    """Choose actual bounded late passages, then rare words within those passages.

    Term rarity is measured across all selected documents, not fabricated anchor
    strings. Ranked recall remains a measured outcome, not a guaranteed success.
    """
    anchors = []
    for record in sorted(records, key=lambda row: (-row["bytes"], row["name"]))[:count]:
        start = max(0, record["bytes"] // 2 - 4096)
        if record["bytes"] > 65536 + 8192:
            start = max(start, 65540)
        with Path(record["path"]).open("rb") as stream:
            stream.seek(start)
            raw = stream.read(32772)
        leading = 0
        while leading < len(raw) and raw[leading] & 0xC0 == 0x80:
            leading += 1
        start += leading
        raw = raw[leading:]
        raw = raw[: boundary(raw, min(len(raw), 32768))]
        text = raw.decode("utf-8")
        candidates, seen_lines = [], set()
        offset = start
        for line_number, line in enumerate(text.splitlines(keepends=True)):
            stripped = line.strip()
            words = re.findall(r"[^\W_]+", stripped, re.UNICODE)
            complete_line = (line_number != 0 or start == 0) and (
                line.endswith(("\n", "\r"))
                or offset + len(line.encode()) == record["bytes"]
            )
            if complete_line and 30 <= len(stripped) <= 200 and 5 <= len(words) <= 30:
                useful = [
                    word.casefold()
                    for word in words
                    if len(word) >= 6 and not word.isdecimal()
                ]
                if len(set(useful)) >= 2 and stripped not in seen_lines:
                    seen_lines.add(stripped)
                    lead = len(line) - len(line.lstrip())
                    passage_start = offset + len(line[:lead].encode())
                    candidates.append(
                        (len(set(useful)), passage_start, stripped, useful)
                    )
            offset += len(line.encode())
        if not candidates:
            continue
        for _, passage_start, passage, useful in sorted(
            candidates, key=lambda row: (-row[0], row[1])
        )[:64]:
            anchors.append(
                {
                    "name": record["name"],
                    "title": record["title"],
                    "literal": passage,
                    "start_byte": passage_start,
                    "end_byte": passage_start + len(passage.encode()),
                    "terms": sorted(set(useful)),
                }
            )
    if not anchors:
        raise ValueError(
            "No suitable real passages found; supply explicit source documents with prose"
        )
    candidates = {word for anchor in anchors for word in anchor["terms"]}
    frequency = Counter()
    for record in records:
        frequency.update(document_terms(record["path"], candidates))
    for anchor in anchors:
        chosen = sorted(
            anchor.pop("terms"), key=lambda word: (frequency[word], -len(word), word)
        )[:2]
        anchor["query"] = " ".join(chosen)
        anchor["term_document_counts"] = {word: frequency[word] for word in chosen}
        anchor["beyond_64kib"] = anchor["start_byte"] > 65536
    chosen_by_name = {}
    for anchor in sorted(
        anchors,
        key=lambda row: (
            sum(row["term_document_counts"].values()),
            max(row["term_document_counts"].values()),
            -sum(map(len, row["query"].split())),
            row["start_byte"],
        ),
    ):
        chosen_by_name.setdefault(anchor["name"], anchor)
    return list(chosen_by_name.values())


def document_terms(path, candidates):
    """Exact candidate-word membership with bounded carry across read edges."""
    seen, tail, skipping = set(), "", False
    maximum = max(map(len, candidates))
    decoder = codecs.getincrementaldecoder("utf-8")()
    with Path(path).open("rb") as stream:
        while block := stream.read(65536):
            text = tail + decoder.decode(block)
            tail = ""
            if skipping:
                ending = re.search(r"[\W_]", text)
                if ending is None:
                    continue
                text, skipping = text[ending.start() :], False
            matches = list(re.finditer(r"[^\W_]+", text))
            if matches and matches[-1].end() == len(text):
                unfinished = matches.pop().group()
                if len(unfinished) > maximum:
                    skipping = True
                else:
                    tail = unfinished
            seen.update(
                word
                for item in matches
                if (word := item.group().casefold()) in candidates
            )
        tail += decoder.decode(b"", final=True)
        if not skipping and tail.casefold() in candidates:
            seen.add(tail.casefold())
    return seen


def chunks(path, expected_digest):
    """Independent streaming chunker for the capable local FTS control."""
    digest, pending, offset = hashlib.sha256(), b"", 0
    with Path(path).open("rb") as stream:
        while True:
            block = stream.read(65536)
            digest.update(block)
            pending += block
            while len(pending) > CHUNK + 3 or (not block and len(pending) > CHUNK):
                end = boundary(pending, CHUNK)
                line_end = pending.rfind(b"\n", CHUNK // 2, end)
                if line_end >= 0:
                    end = line_end + 1
                yield offset, pending[:end].decode("utf-8")
                advance = boundary(pending, end - OVERLAP)
                offset += advance
                pending = pending[advance:]
            if not block:
                if pending:
                    yield offset, pending.decode("utf-8")
                break
    if digest.hexdigest() != expected_digest:
        raise ValueError("Direct FTS source digest changed")


class DirectIndex:
    """Persistent FTS5 control with exact chunks, hashes and incremental updates."""

    def __init__(self, path):
        self.path = Path(path)
        with sqlite3.connect(self.path) as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS docs(name TEXT PRIMARY KEY,digest TEXT,stamp TEXT);
            CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(name UNINDEXED,start UNINDEXED,end UNINDEXED,body,tokenize='unicode61 remove_diacritics 2');
            """)

    def refresh(self, records, max_bytes=128 * MIB, max_versions=128):
        built = used = pending = 0
        with sqlite3.connect(self.path) as db:
            for row in records:
                info = Path(row["path"]).stat()
                stamp = compact(
                    [
                        info.st_dev,
                        info.st_ino,
                        info.st_size,
                        info.st_mtime_ns,
                        info.st_ctime_ns,
                    ]
                )
                old = db.execute(
                    "SELECT digest,stamp FROM docs WHERE name=?", (row["name"],)
                ).fetchone()
                if old == (row["sha256"], stamp):
                    continue
                if built >= max_versions or used + row["bytes"] > max_bytes:
                    pending += 1
                    continue
                if old is not None:
                    db.execute("DELETE FROM search WHERE name=?", (row["name"],))
                for start, body in chunks(row["path"], row["sha256"]):
                    db.execute(
                        "INSERT INTO search VALUES(?,?,?,?)",
                        (row["name"], start, start + len(body.encode()), body),
                    )
                after = Path(row["path"]).stat()
                if (
                    compact(
                        [
                            after.st_dev,
                            after.st_ino,
                            after.st_size,
                            after.st_mtime_ns,
                            after.st_ctime_ns,
                        ]
                    )
                    != stamp
                ):
                    raise ValueError("Direct FTS source changed while indexing")
                db.execute(
                    "INSERT OR REPLACE INTO docs VALUES(?,?,?)",
                    (row["name"], row["sha256"], stamp),
                )
                built, used = built + 1, used + row["bytes"]
        return {
            "indexed_versions": built,
            "indexed_bytes": used,
            "pending_versions": pending,
            "index_complete": pending == 0,
        }

    def search(self, query, limit=5):
        terms = re.findall(r"[^\W_]+", query, re.UNICODE)
        expression = " OR ".join('"' + term + '"' for term in terms)
        with sqlite3.connect(self.path) as db:
            rows = db.execute(
                "SELECT search.name,start,end,body,bm25(search,0,0,0,1) AS score,docs.digest FROM search JOIN docs ON docs.name=search.name WHERE search MATCH ? ORDER BY score,search.name,start LIMIT ?",
                (expression, limit),
            ).fetchall()
        return [
            {
                "name": name,
                "start_byte": start,
                "end_byte": end,
                "excerpt": body,
                "score": score,
                "sha256": digest,
            }
            for name, start, end, body, score, digest in rows
        ]


def verify_hits(hits, sources):
    for hit in hits:
        row = sources[hit.get("artifact_id", hit["name"])]
        start, end = hit["start_byte"], hit["end_byte"]
        if not 0 <= start <= end <= row["bytes"]:
            raise AssertionError("Citation range is outside its exact source")
        with Path(row["path"]).open("rb") as stream:
            stream.seek(start)
            expected = stream.read(end - start)
        if expected != hit["excerpt"].encode("utf-8"):
            raise AssertionError(
                "Citation bytes differ from independently retained source"
            )
        if hit.get("sha256", row["sha256"]) != row["sha256"]:
            raise AssertionError(
                "Citation source hash does not match the acquisition manifest"
            )
    return True


def target_found(hits, query):
    return any(
        hit["name"] == query["name"]
        and hit["start_byte"] <= query["start_byte"]
        and hit["end_byte"] >= query["end_byte"]
        for hit in hits
    )


def literal_probe(workbook, query, records_by_id, max_pages=8, refs=None):
    started, cursor, scanned, hits, calls = time.perf_counter(), None, 0, [], 0
    for _ in range(max_pages):
        reply = search(
            workbook,
            query["literal"],
            refs=refs,
            limit=5,
            context_bytes=256,
            cursor=cursor,
        )
        calls += 1
        scanned += reply["scanned_bytes"]
        hits.extend(reply["hits"])
        verify_hits(reply["hits"], records_by_id)
        cursor = reply["cursor"]
        if hits or cursor is None:
            break
    return {
        "elapsed_seconds": time.perf_counter() - started,
        "calls": calls,
        "scanned_bytes": scanned,
        "exact_citations": True,
        "target_found": target_found(hits, query),
        "search_complete": cursor is None,
        "stopped_after_hit": bool(hits) and cursor is not None,
        "page_limit_reached": bool(cursor) and not hits and calls == max_pages,
        "hits": hits,
    }


def run_suite(
    documents,
    output,
    *,
    manifest=None,
    minimum_bytes=100 * MIB,
    counter,
    query_count=6,
    literal_pages=8,
    build_passes=100,
    bundle_probe=False,
    tokenizer="o200k_base",
):
    helper = helpers()
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output / "workbook" / "workbook.sqlite3").exists():
        raise ValueError("Use a fresh output directory for cold measurements")
    started = time.perf_counter()
    records, acquisition = corpus(documents, manifest, minimum_bytes)
    queries = derive_queries(records, query_count)
    report = {
        "format": 1,
        "method": "Single local lifecycle over real UTF8 sources; indexed FTS5 control plus separate literal exact-anchor diagnostics. Lexical recall is not semantic equivalence.",
        "acquisition": acquisition,
        "documents": records,
        "source_bytes": sum(row["bytes"] for row in records),
        "document_count": len(records),
        "minimum_source_bytes_requested": minimum_bytes,
        "queries_requested": query_count,
        "tokenizer": tokenizer,
        "token_accounting": "Compact native API JSON; excludes MCP envelopes and provider/model usage.",
        "queries": queries,
        "memory_measure": "Cumulative harness-process high-water RSS; not a per-call increment or process-tree sample.",
        "events": [],
        "query_results": [],
    }
    workbook = Workbook(output / "workbook")
    records_by_id, records_by_name = {}, {row["name"]: row for row in records}
    for position, record in enumerate(records):
        saved = workbook.import_path(
            Path(record["path"]),
            name=record["name"],
            summary=record["title"][:360],
            source=record["source_url"][:360],
        )
        record["artifact_id"] = saved["artifact_id"]
        records_by_id[saved["artifact_id"]] = dict(record)
        if (position + 1) % 128 == 0:
            print(
                compact(
                    {
                        "phase": "import",
                        "documents": position + 1,
                        "source_bytes": sum(
                            item["bytes"] for item in records[: position + 1]
                        ),
                    }
                ),
                flush=True,
            )
    report["preparation_seconds"] = time.perf_counter() - started

    def persist():
        report["peak_process_rss_bytes"] = peak_rss()
        helper.write_json(output / "results.json", report)

    direct = DirectIndex(output / "direct.sqlite3")
    for phase in ("direct_cold", "workspool_cold"):
        complete = False
        for page in range(build_passes):
            started = time.perf_counter()
            value = (
                direct.refresh(records)
                if phase == "direct_cold"
                else ranked_search(workbook, queries[0]["query"], limit=5)
            )
            elapsed = time.perf_counter() - started
            if phase == "workspool_cold":
                verify_hits(value["hits"], records_by_id)
            event = {
                "phase": phase,
                "pass": page + 1,
                "elapsed_seconds": elapsed,
                **{
                    key: value[key]
                    for key in (
                        "indexed_versions",
                        "indexed_bytes",
                        "pending_versions",
                        "index_complete",
                    )
                },
            }
            assert (
                event["indexed_bytes"] <= 128 * MIB and event["indexed_versions"] <= 128
            )
            report["events"].append(event)
            print(compact(event), flush=True)
            persist()
            if value["index_complete"]:
                complete = True
                break
            if not value["indexed_versions"]:
                break
        report[phase + "_coverage_complete"] = complete
    if (
        not report["workspool_cold_coverage_complete"]
        or not report["direct_cold_coverage_complete"]
    ):
        report["success"] = False
        persist()
        return report

    for phase in ("warm", "reopened"):
        if phase == "reopened":
            workbook, direct = (
                Workbook(output / "workbook"),
                DirectIndex(output / "direct.sqlite3"),
            )
        for query in queries:
            started = time.perf_counter()
            direct.refresh(records)
            direct_hits = direct.search(query["query"])
            direct_seconds = time.perf_counter() - started
            started = time.perf_counter()
            value = ranked_search(workbook, query["query"], limit=5)
            elapsed = time.perf_counter() - started
            assert value["indexed_versions"] == 0 and value["index_complete"]
            verify_hits(value["hits"], records_by_id)
            verify_hits(direct_hits, records_by_name)
            entry = {
                "phase": phase,
                "query": query["query"],
                "target": query["name"],
                "workspool_seconds": elapsed,
                "direct_seconds": direct_seconds,
                "workspool_target_found": target_found(value["hits"], query),
                "direct_target_found": target_found(direct_hits, query),
                "exact_citations": True,
                "workspool_tokens": counter(compact(value)),
                "direct_tokens": counter(compact({"hits": direct_hits})),
                "workspool": value,
                "direct_hits": direct_hits,
            }
            if phase == "warm":
                entry["literal_corpus"] = literal_probe(
                    workbook, query, records_by_id, literal_pages
                )
                entry["literal_pinned"] = literal_probe(
                    workbook,
                    query,
                    records_by_id,
                    literal_pages,
                    [records_by_name[query["name"]]["artifact_id"]],
                )
            report["query_results"].append(entry)
            persist()

    target = records_by_name[queries[0]["name"]]
    replacement = next(row for row in records if row["sha256"] != target["sha256"])
    saved = workbook.import_path(
        Path(replacement["path"]),
        name=target["name"],
        expected_version=1,
        source=replacement["source_url"][:360],
    )
    changed = dict(replacement) | {
        "name": target["name"],
        "artifact_id": saved["artifact_id"],
    }
    records_by_id[saved["artifact_id"]] = changed
    records_by_name[target["name"]] = changed
    changed_records = [
        changed if row["name"] == target["name"] else row for row in records
    ]
    started = time.perf_counter()
    update = ranked_search(workbook, queries[0]["query"], limit=5)
    update_seconds = time.perf_counter() - started
    verify_hits(update["hits"], records_by_id)
    direct_update = direct.refresh(changed_records)
    assert update["indexed_versions"] == direct_update["indexed_versions"] == 1
    historic = ranked_search(
        workbook, queries[0]["query"], refs=[target["artifact_id"]], limit=5
    )
    verify_hits(historic["hits"], records_by_id)
    assert all(not hit["is_latest"] for hit in historic["hits"])
    report["changed_source"] = {
        "method": "Controlled replacement with another real corpus document, not an authentic new public revision.",
        "workspool_seconds": update_seconds,
        "workspool_indexed_versions": update["indexed_versions"],
        "direct_indexed_versions": direct_update["indexed_versions"],
        "historical_citations_exact": True,
        "historical_target_found": target_found(historic["hits"], queries[0]),
    }

    chosen = queries[1:5] or derive_queries([replacement], 1)
    names = list(dict.fromkeys(query["name"] for query in chosen))[:4]
    define_task(
        workbook,
        "public-text",
        objective="Retrieve exact source passages for the selected research topics.",
        sources={f"doc{i}": name for i, name in enumerate(names)},
        steps=[],
        searches=[query["query"] for query in chosen],
    )
    full = run_task(workbook, "public-text", max_bytes=131072)
    verify_hits(full["citations"], records_by_id)
    reopened = status(Workbook(output / "workbook"), "public-text", max_bytes=131072)
    assert (
        reopened["run_id"] == full["run_id"]
        and reopened["citations"] == full["citations"]
    )
    retained = status(
        workbook, "public-text", base_run=full["run_id"], max_bytes=131072
    )
    report["task_context"] = {
        "state": full["state"],
        "fresh_reset_tokens": counter(compact(reopened)),
        "reset_citations_exact": True,
        "retained_delivery": retained.get("delivery", "full"),
        "retained_tokens": counter(compact(retained)),
        "budget_checks": [],
    }
    for budget in (1000, 2000):
        try:
            reply = status(
                workbook,
                "public-text",
                max_bytes=131072,
                fits=lambda value, limit=budget: counter(compact(value)) <= limit,
            )
            report["task_context"]["budget_checks"].append(
                {
                    "budget": budget,
                    "status": "ok",
                    "tokens": counter(compact(reply)),
                    "brief_complete": reply["brief_complete"],
                    "citations": len(reply["citations"]),
                    "omitted": reply["omitted"],
                }
            )
        except ValueError as error:
            report["task_context"]["budget_checks"].append(
                {"budget": budget, "status": "budget_error", "error": str(error)}
            )
    if bundle_probe:
        report["bundle_probe"] = probe_bundle(records, output, counter)
    report["success"] = (
        full["state"] == "complete" and retained.get("delivery") == "unchanged"
    )
    report["target_recall"] = {
        arm: sum(bool(row[arm + "_target_found"]) for row in report["query_results"])
        / len(report["query_results"])
        for arm in ("workspool", "direct")
    }
    report["source_hashes"] = {
        relative: helper.digest_file(Path(__file__).parents[1] / relative)
        for relative in (
            "benchmarks/public_text_stress.py",
            "src/agent_workbook/context_index.py",
            "src/agent_workbook/retrieval.py",
            "src/agent_workbook/tasks.py",
        )
    }
    persist()
    return report


def probe_bundle(records, output, counter):
    """Real concatenation tests the single-version cap; no repeated filler."""
    path = Path(output) / "derived-corpus-bundle.txt"
    with path.open("wb") as destination:
        for record in records:
            with Path(record["path"]).open("rb") as source:
                for block in iter(lambda: source.read(MIB), b""):
                    destination.write(block)
            destination.write(b"\n")
    size, digest = scan_source(path)
    workbook = Workbook(Path(output) / "bundle-workbook")
    saved = workbook.import_path(path, name="derived-bundle")
    # A real suffix supplies the query; this probe measures coverage, not recall.
    query = derive_queries(records, 1)[0]["query"]
    started = time.perf_counter()
    default = ranked_search(workbook, query, refs=["derived-bundle"], limit=1)
    default_seconds = time.perf_counter() - started
    result = {
        "derived_not_original_document": True,
        "bytes": size,
        "sha256": digest,
        "sources": len(records),
        "default_index_complete": default["index_complete"],
        "default_pending": default["pending_versions"],
        "default_seconds": default_seconds,
    }
    if size > 128 * MIB:
        assert not default["index_complete"] and not default["hits"]
        started = time.perf_counter()
        explicit = ranked_search(
            workbook,
            query,
            refs=["derived-bundle"],
            limit=1,
            max_index_bytes=min(8 * 1024 * MIB, size),
        )
        result.update(
            explicit_index_complete=explicit["index_complete"],
            explicit_seconds=time.perf_counter() - started,
            explicit_tokens=counter(compact(explicit)),
        )
        verify_hits(
            explicit["hits"],
            {
                saved["artifact_id"]: {
                    "path": str(path),
                    "bytes": size,
                    "sha256": digest,
                }
            },
        )
    else:
        result["cap_crossed"] = False
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--documents", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--minimum-mib", type=int, default=100)
    parser.add_argument("--query-count", type=int, default=6)
    parser.add_argument("--literal-pages", type=int, default=8)
    parser.add_argument("--bundle-probe", action="store_true")
    parser.add_argument(
        "--tokenizer", choices=("cl100k_base", "o200k_base"), default="o200k_base"
    )
    args = parser.parse_args()
    if (
        not 1 <= args.query_count <= 8
        or not 1 <= args.literal_pages <= 64
        or not 0 <= args.minimum_mib <= 1024
    ):
        parser.error("Invalid query, page or corpus bounds")
    report = run_suite(
        args.documents,
        args.output,
        manifest=args.manifest,
        minimum_bytes=args.minimum_mib * MIB,
        counter=helpers().load_counter(args.tokenizer),
        query_count=args.query_count,
        literal_pages=args.literal_pages,
        bundle_probe=args.bundle_probe,
        tokenizer=args.tokenizer,
    )
    if not report["success"]:
        raise SystemExit(
            "Public-text stress recorded incomplete coverage/context; inspect results.json"
        )


if __name__ == "__main__":
    main()
