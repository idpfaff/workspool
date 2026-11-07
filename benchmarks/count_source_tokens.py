"""Count every token in the canonical market CSV with bounded working memory.

The input is ASCII CSV with LF endings, alphabetic ticker starts and numeric
final fields. Newline-aligned chunks split both supported tokenizer regexes
between pre-tokens, so their counts add exactly. This counts local text only.
"""

import argparse
import hashlib
import importlib.metadata
import json
import resource
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ENCODINGS = ("cl100k_base", "o200k_base")
VERIFIED_PATTERN_SHA256 = {
    "cl100k_base": "f021c3d976978e62ee64cdad150cc3405c2e3d6e3b40407850bb9e8d9eb65899",
    "o200k_base": "2d1b8dc11e89af71459b36004f698ab3693f59fd84f63e8ec2b49564ab857420",
}


def validate_encoders(encoders):
    """A changed pre-token regex needs a new seam proof before streaming counts."""
    if set(encoders) != set(ENCODINGS):
        raise ValueError("Unverified tokenizer set")
    for name, codec in encoders.items():
        if (
            hashlib.sha256(codec._pat_str.encode()).hexdigest()
            != VERIFIED_PATTERN_SHA256[name]
        ):
            raise ValueError(f"Unverified tokenizer pattern: {name}")


def load_encoders():
    import tiktoken
    import tiktoken.load

    original = tiktoken.load.read_file

    def cached_only(path):
        raise ValueError("Pre-cache the tokenizer rank files before counting")

    try:
        tiktoken.load.read_file = cached_only
        encoders = {name: tiktoken.get_encoding(name) for name in ENCODINGS}
        validate_encoders(encoders)
        return encoders
    finally:
        tiktoken.load.read_file = original


def chunks(stream, chunk_bytes=2**21):
    """Validate every seam and reject inputs outside the proven CSV contract."""
    while block := stream.read(chunk_bytes):
        if not block.endswith(b"\n"):
            block += stream.readline(4096)
        if (
            not block.endswith(b"\n")
            or len(block) < 2
            or not 48 <= block[-2] <= 57
            or not (65 <= block[0] <= 90 or 97 <= block[0] <= 122)
            or b"\r" in block
            or b'"' in block
            or b"\n\n" in block
            or not block.isascii()
        ):
            raise ValueError("Input violates the canonical CSV chunk-boundary contract")
        yield block


def token_counts(block, encoders):
    text = block.decode("ascii")
    return {name: len(codec.encode_ordinary(text)) for name, codec in encoders.items()}


def measure(path, *, workers=4, chunk_bytes=2**21, prefix_rows=1000):
    encoders = load_encoders()
    started = time.perf_counter()
    initial = path.stat()
    digest = hashlib.sha256()
    totals = dict.fromkeys(encoders, 0)
    size = lines = blocks = maximum = 0
    prefix = bytearray()
    prefix_lines = 0
    last_progress = started

    def consume(future):
        for name, value in future.result().items():
            totals[name] += value

    with path.open("rb") as stream, ThreadPoolExecutor(max_workers=workers) as pool:
        pending = deque()
        for block in chunks(stream, chunk_bytes):
            digest.update(block)
            size += len(block)
            lines += block.count(b"\n")
            blocks += 1
            maximum = max(maximum, len(block))
            if prefix_lines < prefix_rows + 1:
                selected = block.splitlines(keepends=True)[
                    : prefix_rows + 1 - prefix_lines
                ]
                prefix.extend(b"".join(selected))
                prefix_lines += len(selected)
            pending.append(pool.submit(token_counts, block, encoders))
            if len(pending) >= workers:
                consume(pending.popleft())
            if time.perf_counter() - last_progress >= 20:
                print(
                    json.dumps({"bytes_read": size, "rows_read": lines - 1}), flush=True
                )
                last_progress = time.perf_counter()
        while pending:
            consume(pending.popleft())
    final = path.stat()
    assert (initial.st_size, initial.st_mtime_ns) == (final.st_size, final.st_mtime_ns)
    assert size == final.st_size and lines > 1 and prefix_lines == prefix_rows + 1
    native_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {
        "source": {
            "bytes": size,
            "rows": lines - 1,
            "sha256": digest.hexdigest(),
            "tokens": totals,
            "headers": 1,
        },
        "prefix": {
            "bytes": len(prefix),
            "rows": prefix_rows,
            "sha256": hashlib.sha256(prefix).hexdigest(),
            "tokens": token_counts(prefix, encoders),
            "headers": 1,
        },
        "method": {
            "tiktoken_version": importlib.metadata.version("tiktoken"),
            "patterns": {name: codec._pat_str for name, codec in encoders.items()},
            "workers": workers,
            "chunks": blocks,
            "maximum_chunk_bytes": maximum,
            "elapsed_seconds": time.perf_counter() - started,
            "peak_process_rss_bytes": native_rss
            if sys.platform == "darwin"
            else native_rss * 1024,
            "exactness": "Every byte is counted. Each checked seam is numeric+LF followed by an ASCII letter; neither recorded regex can form a pre-token across it. BPE merges stay inside pre-tokens. No sampling, byte/token ratio estimate, special-token substitution, or model call.",
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, choices=range(1, 9), default=4)
    args = parser.parse_args()
    result = measure(args.source, workers=args.workers)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result), flush=True)
