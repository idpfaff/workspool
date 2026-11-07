"""Deterministic MCP comparisons against an isolated v0.4.1 source archive.

Run with tiktoken installed, from the repository root:
    python benchmarks/roundtrip_efficiency.py --output .local-benchmarks/roundtrips

All fixtures are synthetic. Independent requests share one scripted decision.
Payload counts and retained-prefix exposure are not model inference or billing.
"""

import argparse
import asyncio
import hashlib
import importlib.metadata
import io
import json
import os
import platform
import random
import re
import shutil
import subprocess
import sys
import tarfile
import time
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import UUID

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent_workbook.render import decode
from agent_workbook.store import Workbook


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def sha(value):
    return hashlib.sha256(value).hexdigest()


class Counts:
    def __init__(self):
        import tiktoken

        self.encoders = {
            name: tiktoken.get_encoding(name) for name in ("cl100k_base", "o200k_base")
        }
        self.text = lru_cache(maxsize=4096)(self._text)

    def _text(self, text):
        return {
            name: len(encoder.encode(text, disallowed_special=()))
            for name, encoder in self.encoders.items()
        }

    def parts(self, texts):
        total = dict.fromkeys(self.encoders, 0)
        for text in texts:
            for name, count in self.text(text).items():
                total[name] += count
        return total


class Trace:
    def __init__(self, name, counts):
        self.name, self.counts = name, counts
        self.setup, self.events, self.decisions, self.results = [], [], [], []
        self.tool_seconds = 0.0

    def add(self, role, value):
        self.events.append(
            {"role": role, "text": value if isinstance(value, str) else compact(value)}
        )

    def decision(self):
        self.decisions.append(len(self.events))

    def answer(self, value):
        self.decision()
        self.add("assistant_answer", value)

    def measure(self):
        setup = self.counts.parts(self.setup)
        messages = [self.counts.parts([event["text"]]) for event in self.events]
        return {
            "new_payload_tokens": {
                codec: setup[codec] + sum(message[codec] for message in messages)
                for codec in setup
            },
            "retained_input_exposure": {
                codec: sum(
                    setup[codec] + sum(message[codec] for message in messages[:end])
                    for end in self.decisions
                )
                for codec in setup
            },
            "setup_tokens": setup,
            "instruction_tokens": self.counts.parts(self.setup[:1]),
            "schema_tokens": self.counts.parts(self.setup[1:]),
            "tool_request_tokens": self.counts.parts(
                [
                    event["text"]
                    for event in self.events
                    if event["role"] == "assistant_tool_call"
                ]
            ),
            "tool_reply_tokens": self.counts.parts(
                [event["text"] for event in self.events if event["role"] == "tool"]
            ),
            "user_tokens": self.counts.parts(
                [event["text"] for event in self.events if event["role"] == "user"]
            ),
            "answer_tokens": self.counts.parts(
                [
                    event["text"]
                    for event in self.events
                    if event["role"] == "assistant_answer"
                ]
            ),
            "tool_calls": len(self.results),
            "scripted_decisions": len(self.decisions),
            "tool_seconds": round(self.tool_seconds, 6),
            "max_reply_bytes": max(
                (
                    len(event["text"].encode())
                    for event in self.events
                    if event["role"] == "tool"
                ),
                default=0,
            ),
        }

    def dump(self):
        return {
            "name": self.name,
            "setup": self.setup,
            "events": self.events,
            "decision_prefix_lengths": self.decisions,
            "results": self.results,
            "measured": self.measure(),
        }


@asynccontextmanager
async def session(source, workspace, trace, tools):
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "agent_workbook.cli",
            "--workspace",
            str(workspace),
            "serve",
            "--tools",
            *tools,
            "--response-format",
            "adaptive",
            "--response-content",
            "text",
        ],
        env=dict(os.environ, PYTHONPATH=str(source / "src")),
    )
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as client,
    ):
        initialized = await client.initialize()
        definitions = await client.list_tools()
        assert {tool.name for tool in definitions.tools} == set(tools)
        trace.setup = [
            initialized.instructions or "",
            compact(
                [
                    tool.model_dump(by_alias=True, exclude_none=True)
                    for tool in definitions.tools
                ]
            ),
        ]
        yield client


async def calls(client, trace, requests):
    """One planned assistant decision can issue multiple independent requests."""
    assert requests
    trace.decision()
    for name, arguments in requests:
        trace.add("assistant_tool_call", {"name": name, "arguments": arguments})
    started = time.perf_counter()
    replies = await asyncio.gather(
        *(client.call_tool(name, arguments) for name, arguments in requests)
    )
    trace.tool_seconds += time.perf_counter() - started
    data = []
    for (name, arguments), reply in zip(requests, replies, strict=True):
        assert not reply.isError, (name, arguments, reply.content)
        assert all(block.type == "text" for block in reply.content)
        trace.add("tool", "\n".join(block.text for block in reply.content))
        trace.results.append(reply.model_dump(by_alias=True, exclude_none=True))
        # Decode the text seen by the host, never rely on hidden structured data.
        data.append(decode(reply.content))
    return data


async def call(client, trace, name, arguments):
    return (await calls(client, trace, [(name, arguments)]))[0]


def save_fixture(workbook, seed, name, text, version=1):
    """Stable, uniformly hashed UUIDs make both arms' received IDs identical."""
    identifier = UUID(hex=sha(f"{seed}/{name}/{version}".encode())[:32])
    with patch("agent_workbook.store.uuid4", return_value=identifier):
        return workbook.save_text(
            text,
            name=name,
            kind="document",
            media_type="text/plain",
            expected_version=version - 1,
        )


def source_text(lines, seed=5):
    rng = random.Random(seed)
    return "".join(
        f"Line {index:04d}: verify record {rng.randrange(10000, 99999)} against retained evidence.\n"
        for index in range(lines)
    )


def apply_changes(body, changes):
    lines = body.splitlines(keepends=True)
    end = 0
    for change in changes:
        start, delete = change["start"], change["delete"]
        assert type(start) is int and type(delete) is int
        assert start >= end and delete >= 0 and start + delete <= len(lines)
        assert all(isinstance(line, str) for line in change["lines"])
        end = start + delete
    for change in reversed(changes):
        lines[change["start"] : change["start"] + change["delete"]] = change["lines"]
    return "".join(lines)


def expected_page(body, offset, length):
    raw = body.encode()
    assert 0 <= offset <= len(raw)
    raw[:offset].decode("utf-8")  # The requested start must be a boundary.
    end = min(len(raw), offset + length)
    text = raw[offset:end].decode("utf-8", errors="ignore")
    actual_end = offset + len(text.encode())
    return text, None if actual_end == len(raw) else actual_end


def inspect_groups(reads):
    groups = defaultdict(list)
    for item in reads:
        key = (item.get("offset", 0), item.get("bytes", 1024))
        if item["ref"] not in groups[key]:
            groups[key].append(item["ref"])
    return [
        (offset, length, refs[start : start + 16])
        for (offset, length), refs in groups.items()
        for start in range(0, len(refs), 16)
    ]


async def inspect_reads(client, trace, reads):
    groups = inspect_groups(reads)
    replies = await calls(
        client,
        trace,
        [
            (
                "inspect",
                {
                    "ref": refs[0] if len(refs) == 1 else refs,
                    "excerpt_bytes": length,
                }
                | ({"offset": offset} if offset else {}),
            )
            for offset, length, refs in groups
        ],
    )
    indexed = {}
    for (offset, length, refs), reply in zip(groups, replies, strict=True):
        artifacts = [reply] if len(refs) == 1 else reply["artifacts"]
        assert len(artifacts) == len(refs)
        for ref, artifact in zip(refs, artifacts, strict=True):
            indexed[(ref, offset, length)] = {
                "artifact": artifact,
                "excerpt": artifact["excerpt"],
                "start_byte": offset,
                "end_byte": offset + len(artifact["excerpt"].encode()),
                "next_offset": artifact["next_offset"],
            }
    return [
        indexed[(item["ref"], item.get("offset", 0), item.get("bytes", 1024))]
        for item in reads
    ]


def select_span(reply, entry):
    span = reply["spans"][entry["span"]]
    artifact = reply["artifacts"][span["artifact"]]
    start, end = entry["start_byte"], entry["end_byte"]
    assert span["start_byte"] <= start <= end <= span["end_byte"]
    raw = span["excerpt"].encode()
    assert len(raw) == span["end_byte"] - span["start_byte"]
    excerpt = raw[start - span["start_byte"] : end - span["start_byte"]].decode("utf-8")
    return {
        "artifact": artifact,
        "excerpt": excerpt,
        "start_byte": start,
        "end_byte": end,
        "next_offset": entry.get("next_offset"),
    }


async def retrieve_reads(client, trace, reads):
    result = await call(client, trace, "retrieve", {"reads": reads})
    assert len(result["reads"]) == len(reads)
    return [select_span(result, entry) for entry in result["reads"]]


async def whole_read(client, trace, ref):
    body, offset, pinned, metadata = "", 0, ref, None
    while True:
        item = (
            await inspect_reads(
                client, trace, [{"ref": pinned, "offset": offset, "bytes": 8192}]
            )
        )[0]
        metadata = item["artifact"]
        pinned = metadata["artifact_id"]
        assert item["start_byte"] == len(body.encode())
        body += item["excerpt"]
        offset = item["next_offset"]
        if offset is None:
            return metadata, body


async def sync_read(client, trace, ref, prior=None, body=None):
    args = {"ref": ref}
    if prior is not None:
        assert body is not None
        args["base"] = prior
    reply = await call(client, trace, "sync", args)
    mode, artifact = reply["mode"], reply["artifact"]
    if mode in {"unchanged", "delta"}:
        assert body is not None and reply["complete"]
        if mode == "delta":
            assert reply["base"] == prior
        return (
            artifact,
            body if mode == "unchanged" else apply_changes(body, reply["changes"]),
            mode,
        )
    assert mode == "full" and reply["offset"] == 0
    body = reply["excerpt"]
    while not reply["complete"]:
        offset = reply["next_offset"]
        assert offset == len(body.encode())
        reply = await call(
            client, trace, "sync", {"ref": artifact["artifact_id"], "offset": offset}
        )
        assert reply["mode"] == "full" and reply["offset"] == offset
        assert reply["artifact"]["artifact_id"] == artifact["artifact_id"]
        body += reply["excerpt"]
    assert reply["next_offset"] is None
    return artifact, body, mode


def record_case(case, arm, trace, traces, verification):
    traces.append(trace.dump())
    result = {
        "case": case,
        "arm": arm,
        "success": True,
        "measured": trace.measure(),
        "verification": verification,
    }
    print(case, arm, compact(result["measured"]["new_payload_tokens"]), flush=True)
    return result


async def sync_cases(root, baseline, candidate, counts, traces):
    cases = []
    prompt = "Synchronize every byte of operations/runbook for a subsequent full-document edit. Report its version and freshness."
    for case in ("sync_sparse", "sync_dense", "sync_tiny"):
        texts = [
            f"INSTRUCTION=route-{revision}\n"
            + (
                ""
                if case == "sync_tiny"
                else source_text(80, 5 + (revision if case == "sync_dense" else 0))
            )
            for revision in range(3)
        ]
        fixture = root / (case + "-fixture")
        wb = Workbook(fixture)
        save_fixture(wb, case, "operations/runbook", texts[0])
        save_fixture(wb, case, "unrelated", "unrelated")
        arms = ["metadata_first", "diff_first", "sync"]
        if case == "sync_tiny":
            arms.insert(0, "direct_read")
        for arm in arms:
            work = root / (case + "-" + arm)
            shutil.copytree(fixture, work)
            wb = Workbook(work)
            trace = Trace(case + "/" + arm, counts)
            tools = (
                ["sync"]
                if arm == "sync"
                else ["inspect"] + (["diff"] if arm == "diff_first" else [])
            )
            prior, body, modes, verified = None, None, Counter(), []
            async with session(
                candidate if arm == "sync" else baseline, work, trace, tools
            ) as client:
                for turn in range(8):
                    revision = 0 if turn < 4 else 1 if turn < 6 else 2
                    if turn == 2:
                        save_fixture(
                            wb, case, "unrelated", "changed unrelated", version=2
                        )
                    if turn in (4, 6):
                        save_fixture(
                            wb,
                            case,
                            "operations/runbook",
                            texts[revision],
                            version=revision + 1,
                        )
                    trace.add("user", prompt)
                    if arm == "sync":
                        metadata, body, mode = await sync_read(
                            client, trace, "operations/runbook", prior, body
                        )
                    elif prior is None or arm == "direct_read":
                        metadata, body = await whole_read(
                            client, trace, "operations/runbook"
                        )
                        mode = "full"
                    elif arm == "metadata_first":
                        metadata = await call(
                            client, trace, "inspect", {"ref": "operations/runbook"}
                        )
                        mode = "unchanged"
                        if metadata["artifact_id"] != prior:
                            metadata, body = await whole_read(
                                client, trace, metadata["artifact_id"]
                            )
                            mode = "full"
                    else:
                        delta = await call(
                            client,
                            trace,
                            "diff",
                            {"ref": "operations/runbook", "base": prior},
                        )
                        assert delta["base"] == prior
                        metadata = delta["artifact"]
                        if delta.get("requires_read"):
                            metadata, body = await whole_read(
                                client, trace, metadata["artifact_id"]
                            )
                            mode = "fallback_full"
                        else:
                            body = apply_changes(body, delta["changes"])
                            mode = "delta" if delta["changes"] else "unchanged"
                    assert body.encode() == texts[revision].encode()
                    assert metadata["version"] == revision + 1
                    assert metadata["is_latest"] and not metadata["stale_inputs"]
                    prior = (
                        metadata["version"]
                        if arm == "sync"
                        else metadata["artifact_id"]
                    )
                    verified.append(sha(body.encode()))
                    modes[mode] += 1
                    trace.answer(
                        {
                            "version": metadata["version"],
                            "stale_inputs": metadata["stale_inputs"],
                        }
                    )
            cases.append(
                record_case(
                    case,
                    arm,
                    trace,
                    traces,
                    {"complete_source_sha256_by_turn": verified, "modes": dict(modes)},
                )
            )
    return cases


def validate_reads(reads, output, originals, *, allow_prefix=False):
    assert len(output) == len(reads)
    for request, item in zip(reads, output, strict=True):
        name = item["artifact"]["name"]
        assert request["ref"] in {name, item["artifact"]["artifact_id"]}
        expected, next_offset = expected_page(
            originals[name], request.get("offset", 0), request.get("bytes", 1024)
        )
        assert item["start_byte"] == request.get("offset", 0)
        assert item["end_byte"] == item["start_byte"] + len(item["excerpt"].encode())
        if allow_prefix:
            assert expected.startswith(item["excerpt"])
            assert item["next_offset"] == (
                None
                if item["end_byte"] == len(originals[name].encode())
                else item["end_byte"]
            )
        else:
            assert item["excerpt"] == expected and item["next_offset"] == next_offset
        assert item["artifact"]["is_latest"] and not item["artifact"]["stale_inputs"]


async def collect_reads(client, trace, reads, texts, operation, *, complete=False):
    """Retain exact requested ranges even when one budget defers some read slots."""
    retained, pinned, pages = ["" for _ in reads], [None for _ in reads], []
    ends = [
        len(texts[item["ref"]].encode())
        if complete
        else item.get("offset", 0)
        + len(
            expected_page(
                texts[item["ref"]], item.get("offset", 0), item.get("bytes", 1024)
            )[0].encode()
        )
        for item in reads
    ]
    active = list(enumerate(reads))
    while active:
        requests = [request for _, request in active]
        output = await operation(client, trace, requests)
        validate_reads(requests, output, texts, allow_prefix=True)
        following = []
        progress = 0
        for (index, request), item in zip(active, output, strict=True):
            identifier = item["artifact"]["artifact_id"]
            assert pinned[index] in (None, identifier)
            pinned[index] = identifier
            assert item["start_byte"] == reads[index].get("offset", 0) + len(
                retained[index].encode()
            )
            retained[index] += item["excerpt"]
            progress += len(item["excerpt"].encode())
            pages.append(sha(item["excerpt"].encode()))
            end = item["end_byte"]
            assert end <= ends[index]
            if end < ends[index]:
                assert item["next_offset"] == end
                following.append(
                    (
                        index,
                        {
                            "ref": identifier,
                            "offset": end,
                            "bytes": 8192 if complete else ends[index] - end,
                        },
                    )
                )
        assert progress or not following, "Read continuation made no progress"
        active = following
    expected = [
        texts[item["ref"]]
        if complete
        else expected_page(
            texts[item["ref"]], item.get("offset", 0), item.get("bytes", 1024)
        )[0]
        for item in reads
    ]
    assert retained == expected
    return retained, pages


async def read_cases(root, baseline, candidate, counts, traces):
    cases = []
    for case in ("uneven_reads", "read_all", "tiny_read"):
        fixture = root / (case + "-fixture")
        wb = Workbook(fixture)
        texts = {
            "docs/a": source_text(20),
            "docs/b": source_text(25, 6),
            "docs/c": source_text(30, 7),
        }
        if case == "tiny_read":
            texts = {"docs/a": "Small exact note. 😀\n"}
        if case == "read_all":
            texts = {
                name: source_text(lines, index + 1) + "Unicode tail 😀é\n"
                for index, (name, lines) in enumerate(
                    zip(
                        ("docs/a", "docs/b", "docs/c", "docs/d"),
                        (13, 90, 250, 380),
                        strict=True,
                    )
                )
            }
            texts["docs/empty"] = ""
        for name, body in texts.items():
            save_fixture(wb, case, name, body)
        reads = (
            [
                {"ref": "docs/a", "offset": 0, "bytes": 512},
                {"ref": "docs/a", "offset": 384, "bytes": 512},
                {"ref": "docs/b", "offset": 96, "bytes": 256},
                {"ref": "docs/c", "offset": 0, "bytes": 512},
            ]
            if case == "uneven_reads"
            else [{"ref": name, "bytes": 8192} for name in texts]
        )
        prompt = (
            "Read these exact byte ranges for later review: "
            if case == "uneven_reads"
            else "Read and retain the complete text of these sources: "
        ) + compact(reads)
        for arm, source in (("batched_inspect", baseline), ("retrieve", candidate)):
            work = root / (case + "-" + arm)
            shutil.copytree(fixture, work)
            trace = Trace(case + "/" + arm, counts)
            trace.add("user", prompt)
            async with session(
                source,
                work,
                trace,
                ["inspect"] if arm == "batched_inspect" else ["retrieve"],
            ) as client:
                retained, pages = await collect_reads(
                    client,
                    trace,
                    reads,
                    texts,
                    inspect_reads if arm == "batched_inspect" else retrieve_reads,
                    complete=case != "uneven_reads",
                )
                trace.answer(
                    {"ranges_verified": len(reads)}
                    if case == "uneven_reads"
                    else {"complete_sources": len(retained)}
                )
            cases.append(
                record_case(
                    case,
                    arm,
                    trace,
                    traces,
                    {
                        "exact_page_sha256": pages,
                        "requested_range_sha256": [
                            sha(body.encode()) for body in retained
                        ],
                        "source_sha256": {
                            name: sha(text.encode()) for name, text in texts.items()
                        },
                    },
                )
            )
    return cases


async def sparse_range_cases(root, baseline, candidate, counts, traces):
    """An immutable source and exact disjoint ranges are supplied by the user."""
    fixture = root / "sparse-ranges-fixture"
    name, text = "logs/large", source_text(1800, 27)
    assert len(text.encode()) > 100_000
    artifact = save_fixture(Workbook(fixture), "sparse-ranges", name, text)
    ref = artifact["artifact_id"]
    cases = []
    for count in (1, 4, 16):
        case = f"sparse_ranges_{count}"
        reads = [
            {"ref": ref, "offset": 1024 + index * 6144, "bytes": 256}
            for index in range(count)
        ]
        assert reads[-1]["offset"] + 256 < len(text.encode())
        assert sum(item["bytes"] for item in reads) < len(text.encode()) / 20
        prompt = (
            "Read and retain these exact byte ranges from the supplied immutable source "
            "for later review: " + compact(reads)
        )
        for arm, source in (("batched_inspect", baseline), ("retrieve", candidate)):
            work = root / (case + "-" + arm)
            shutil.copytree(fixture, work)
            trace = Trace(case + "/" + arm, counts)
            trace.add("user", prompt)
            async with session(
                source,
                work,
                trace,
                ["inspect"] if arm == "batched_inspect" else ["retrieve"],
            ) as client:
                retained, pages = await collect_reads(
                    client,
                    trace,
                    reads,
                    {name: text, ref: text},
                    inspect_reads if arm == "batched_inspect" else retrieve_reads,
                )
                trace.answer({"ranges_verified": count})
            cases.append(
                record_case(
                    case,
                    arm,
                    trace,
                    traces,
                    {
                        "source_sha256": {name: sha(text.encode())},
                        "source_bytes": len(text.encode()),
                        "requested_bytes": sum(item["bytes"] for item in reads),
                        "requested_range_sha256": [
                            sha(body.encode()) for body in retained
                        ],
                        "exact_page_sha256": pages,
                    },
                )
            )
    return cases


def passage_fixture(workbook, *, dispersed=False):
    texts, truth = {}, {}
    for index, project in enumerate(("atlas", "orion", "zephyr")):
        facts = {
            "renewal_code": f"RC-{7101 + index}",
            "owner": f"team-{index + 1}",
            "status": "approved",
            "risk_limit": str(150 + index * 25),
        }
        text = (
            source_text(12, index)
            + "\n"
            + "\n".join(
                f"{key}={facts[key]}" for key in ("renewal_code", "owner", "status")
            )
            + "\n"
            + source_text(25, index + 5)
            + f"risk_limit={facts['risk_limit']}\n"
            + source_text(5, index + 10)
        )
        if dispersed:
            text = source_text(12, index) + "\n"
            for position, (field, value) in enumerate(facts.items()):
                text += f"{field}={value}\n" + source_text(20, index + position + 20)
        name = "reviews/" + project
        save_fixture(workbook, "dispersed" if dispersed else "passages", name, text)
        texts[name], truth[name] = text, facts
    return texts, truth


def match_truth(texts, query):
    found = set()
    pattern = re.compile("(?=(" + re.escape(query) + "))", re.IGNORECASE)
    for name, text in texts.items():
        for match in pattern.finditer(text):
            start, end = match.span(1)
            found.add((name, len(text[:start].encode()), len(text[:end].encode())))
    return found


async def search_evidence(client, trace, queries, arm, texts):
    active = list(enumerate(queries))
    matches = [set() for _ in queries]
    evidence, raw_hits = [], []
    while active:
        if arm == "retrieve":
            result = await call(
                client, trace, "retrieve", {"queries": [query for _, query in active]}
            )
            replies = result["queries"]
            for span in result["spans"]:
                artifact = result["artifacts"][span["artifact"]]
                assert (
                    span["excerpt"].encode()
                    == texts[artifact["name"]].encode()[
                        span["start_byte"] : span["end_byte"]
                    ]
                )
                evidence.append((artifact["name"], span["excerpt"]))
        else:
            replies = await calls(
                client,
                trace,
                [
                    (
                        "search",
                        query
                        | {
                            "context_bytes": 0
                            if arm == "search_then_read"
                            else query.get("context_bytes", 256)
                        },
                    )
                    for _, query in active
                ],
            )
        following = []
        for (index, query), reply in zip(active, replies, strict=True):
            if arm == "retrieve":
                for match in reply["matches"]:
                    item = select_span(result, match)
                    matches[index].add(
                        (item["artifact"]["name"], item["start_byte"], item["end_byte"])
                    )
            else:
                for hit in reply["hits"]:
                    name = hit["name"]
                    assert (
                        hit["excerpt"].encode()
                        == texts[name].encode()[hit["start_byte"] : hit["end_byte"]]
                    )
                    matches[index].add(
                        (name, hit["match_start_byte"], hit["match_end_byte"])
                    )
                    evidence.append((name, hit["excerpt"]))
                    raw_hits.append(hit)
            if not reply["complete"]:
                assert reply["cursor"]
                following.append((index, query | {"cursor": reply["cursor"]}))
        active = following
    for query, observed in zip(queries, matches, strict=True):
        assert observed == match_truth(texts, query["text"])
    if arm == "search_then_read":
        ranges = defaultdict(list)
        for hit in raw_hits:
            ranges[hit["artifact_id"]].append(
                (hit["match_start_byte"], hit["match_end_byte"] + 256)
            )
        reads = []
        for ref, intervals in ranges.items():
            merged = []
            for start, end in sorted(intervals):
                if merged and start <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], end)
                else:
                    merged.append([start, end])
            reads.extend(
                {"ref": ref, "offset": start, "bytes": min(8192, end - start)}
                for start, end in merged
            )
        output = await inspect_reads(client, trace, reads)
        validate_reads(reads, output, texts)
        evidence.extend((item["artifact"]["name"], item["excerpt"]) for item in output)
    return evidence


def absorb(cache, evidence):
    for name, excerpt in evidence:
        for match in re.finditer(
            r"(?m)^(renewal_code|owner|status|risk_limit)=([^\r\n]+)", excerpt
        ):
            old = cache[name].get(match[1])
            assert old is None or old == match[2]
            cache[name][match[1]] = match[2]


async def run_passage_case(root, baseline, candidate, counts, traces, *, dispersed):
    case = "dispersed_passages" if dispersed else "overlapping_passages"
    fixture = root / (case + "-fixture")
    texts, truth = passage_fixture(Workbook(fixture), dispersed=dispersed)
    cases = []
    arms = ["batched_search", "incremental_search", "retrieve"]
    if not dispersed:
        arms.insert(1, "search_then_read")
    for arm in arms:
        work = root / (case + "-" + arm)
        shutil.copytree(fixture, work)
        trace = Trace(case + "/" + arm, counts)
        tools = (
            ["retrieve"]
            if arm == "retrieve"
            else ["search"] + (["inspect"] if arm == "search_then_read" else [])
        )
        cache = defaultdict(dict)
        answers = []
        async with session(
            candidate if arm == "retrieve" else baseline, work, trace, tools
        ) as client:
            for fields in (
                ("renewal_code", "owner", "status"),
                ("owner",),
                ("risk_limit",),
            ):
                trace.add(
                    "user",
                    "For atlas, orion, and zephyr, return "
                    + ", ".join(fields)
                    + ". Reuse already read evidence from the same pinned versions.",
                )
                # The incremental policy is fixed before reading any source:
                # search one missing field, absorb context, then reassess.
                groups = (
                    [(field,) for field in fields]
                    if arm == "incremental_search"
                    else [fields]
                )
                for group in groups:
                    missing = [
                        field
                        for field in group
                        if not cache
                        or any(field not in facts for facts in cache.values())
                    ]
                    if missing:
                        queries = [
                            {"text": field + "=", "context_bytes": 256, "limit": 5}
                            for field in missing
                        ]
                        absorb(
                            cache,
                            await search_evidence(client, trace, queries, arm, texts),
                        )
                answer = {
                    name: {field: cache[name][field] for field in fields}
                    for name in sorted(cache)
                }
                expected = {
                    name: {field: truth[name][field] for field in fields}
                    for name in sorted(truth)
                }
                assert answer == expected
                answers.append(answer)
                trace.answer(answer)
        cases.append(
            record_case(
                case,
                arm,
                trace,
                traces,
                {
                    "answers": answers,
                    "source_sha256": {
                        name: sha(text.encode()) for name, text in texts.items()
                    },
                },
            )
        )
    return cases


async def passage_cases(root, baseline, candidate, counts, traces):
    cases = []
    for dispersed in (False, True):
        cases += await run_passage_case(
            root, baseline, candidate, counts, traces, dispersed=dispersed
        )
    return cases


def hashes(paths):
    return {
        str(path.resolve().relative_to(Path.cwd())): sha(path.read_bytes())
        for path in paths
    }


def versions():
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "packages": {
            name: importlib.metadata.version(name)
            for name in ("mcp", "duckdb", "pydantic", "tiktoken")
        },
    }


def archive_baseline(ref, destination):
    commit = subprocess.check_output(
        ["git", "rev-parse", ref + "^{commit}"], text=True
    ).strip()
    archive = subprocess.check_output(["git", "archive", commit, "src"])
    with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
        stream.extractall(destination, filter="data")
    return commit, sha(archive)


async def run(args):
    counts, traces = Counts(), []
    runtime = versions()
    paths = [Path(__file__), *sorted(Path("src/agent_workbook").glob("*.py"))]
    before = hashes(paths)
    with TemporaryDirectory(prefix="workspool-roundtrips-") as directory:
        root = Path(directory)
        baseline = root / "baseline"
        baseline.mkdir()
        commit, archive_hash = await asyncio.to_thread(
            archive_baseline, args.baseline, baseline
        )
        results = []
        for group, runner in (
            ("sync", sync_cases),
            ("reads", read_cases),
            ("sparse_ranges", sparse_range_cases),
            ("passages", passage_cases),
        ):
            if group in args.scenarios:
                results += await runner(root, baseline, Path.cwd(), counts, traces)
        baseline_sources = {
            str(path.relative_to(baseline)): sha(path.read_bytes())
            for path in sorted((baseline / "src").rglob("*.py"))
        }
    assert before == hashes(paths), "Sources changed during measurement"
    assert runtime == versions(), "Runtime packages changed during measurement"
    comparisons = {}
    for case in sorted({item["case"] for item in results}):
        items = [item for item in results if item["case"] == case]
        candidate = next(item for item in items if item["arm"] in {"sync", "retrieve"})
        controls = [item for item in items if item is not candidate]
        comparisons[case] = {}
        for control in controls:
            comparisons[case][control["arm"]] = {
                metric: {
                    codec: round(
                        100
                        * (
                            1
                            - candidate["measured"][metric][codec]
                            / control["measured"][metric][codec]
                        ),
                        2,
                    )
                    for codec in counts.encoders
                }
                for metric in ("new_payload_tokens", "retained_input_exposure")
            }
    report = {
        "baseline_commit": commit,
        "baseline_archive_sha256": archive_hash,
        "baseline_source_sha256": baseline_sources,
        "candidate_source_sha256": before,
        "runtime": runtime,
        "tokenizers": list(counts.encoders),
        "cases": results,
        "comparisons_pct": comparisons,
        "scope": "Actual MCP instructions, cold tool schemas, requests, and formatted text replies. Synthetic fixed plans; independent requests share a decision. Retained exposure replays static setup and all prior canonical message payloads at each planned decision, excluding provider framing, prompt caching, reasoning, inference, and billing. No model reliability claims.",
        "controls": "Both arms use minimal eager tool allowlists, adaptive text-only replies, and the same interpreter/dependencies. Metadata-first and diff-first are separate whole-workflow baselines; tiny sources also have an always-read-full control. No per-turn hindsight selection. Search controls include grouped independent queries, merged reads after context-free matches, and incremental search that reassesses missing facts after each field. Reads batch shared offsets and lengths. Previously seen facts are reused without a new tool call. Sync uses the prior response's version integer as its retained base; baseline diff requires the retained immutable ID.",
        "fixtures": "Synthetic sources imported before measurement; acquisition/import cost excluded. Fixture UUIDs are deterministic valid hashes, identical across implementations, with no server changes. Full-body synchronization and read-all cases verify complete exact bytes, not just facts near the start. Sparse-range cases request 1, 4, or 16 disjoint 256-byte ranges from the same supplied immutable 104400-byte source; both implementations use that ID, and requested bytes remain below 4% of the file. Passage cases retain both adjacent facts and facts separated by over 1 KiB.",
        "timing": "tool_seconds is observed wall time for each request group, excluding process startup. Single local runs are diagnostics, not statistically reliable latency claims.",
    }
    args.output.mkdir(parents=True, exist_ok=True)
    trace_bytes = (json.dumps(traces, ensure_ascii=False, indent=2) + "\n").encode()
    report["traces_sha256"] = sha(trace_bytes)
    (args.output / "traces.json").write_bytes(trace_bytes)
    (args.output / "results.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(comparisons, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default="v0.4.1")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--scenarios",
        nargs="+",
        choices=["sync", "reads", "sparse_ranges", "passages"],
        default=["sync", "reads", "sparse_ranges", "passages"],
    )
    asyncio.run(run(parser.parse_args()))
