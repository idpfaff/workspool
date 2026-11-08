"""Explicit, local token accounting for serialized MCP results."""

import base64
import gzip
import hashlib
import json
import zlib
from functools import lru_cache
from importlib.resources import files
from pathlib import Path

# Pinned from OpenAI tiktoken 0.14.0, under the bundled MIT notice.
# Construct Encodings directly: tiktoken.get_encoding can fetch ranks from a CDN.
_ENCODINGS = {
    "cl100k_base": {
        "sha256": "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7",
        "bytes": 1681126,
        "pat_str": (
            r"'(?i:[sdmt]|ll|ve|re)|[^\r\n\p{L}\p{N}]?+\p{L}++|\p{N}{1,3}+"
            r"| ?[^\s\p{L}\p{N}]++[\r\n]*+|\s++$|\s*[\r\n]|\s+(?!\S)|\s"
        ),
        "special_tokens": {
            "<|endoftext|>": 100257,
            "<|fim_prefix|>": 100258,
            "<|fim_middle|>": 100259,
            "<|fim_suffix|>": 100260,
            "<|endofprompt|>": 100276,
        },
    },
    "o200k_base": {
        "sha256": "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d",
        "bytes": 3613922,
        "pat_str": (
            r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]*"
            r"[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?"
            r"|[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]+"
            r"[\p{Ll}\p{Lm}\p{Lo}\p{M}]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?"
            r"|\p{N}{1,3}| ?[^\s\p{L}\p{N}]+[\r\n/]*|\s*[\r\n]+|\s+(?!\S)|\s+"
        ),
        "special_tokens": {"<|endoftext|>": 199999, "<|endofprompt|>": 200018},
    },
}


def _bundled_ranks(name):
    spec = _ENCODINGS[name]
    resource = files("agent_workbook").joinpath("_tokenizers", name + ".tiktoken.gz")
    try:
        with resource.open("rb") as source, gzip.GzipFile(fileobj=source) as stream:
            data = stream.read(spec["bytes"] + 1)
        if (
            len(data) != spec["bytes"]
            or hashlib.sha256(data).hexdigest() != spec["sha256"]
        ):
            raise ValueError("Tokenizer rank hash mismatch")
    except (OSError, EOFError, ValueError, zlib.error) as error:
        raise ValueError(
            f"Bundled {name} tokenizer data is missing or corrupt; reinstall agent-workbook"
        ) from error
    # The hash pins the complete, upstream-validated rank format and contents.
    return {
        base64.b64decode(token, validate=True): int(rank)
        for token, rank in (line.split() for line in data.splitlines())
    }


@lru_cache(maxsize=2)
def named_encoding(name):
    """Return an exact bundled tiktoken Encoding without cache reads or downloads."""
    if name not in _ENCODINGS:
        raise ValueError("Bundled tokenizer must be cl100k_base or o200k_base")
    try:
        import tiktoken
    except ImportError:
        raise ValueError("Install the tokens extra to use --tokenizer") from None
    spec = _ENCODINGS[name]
    return tiktoken.Encoding(
        name=name,
        pat_str=spec["pat_str"],
        mergeable_ranks=_bundled_ranks(name),
        special_tokens=dict(spec["special_tokens"]),
    )


def serialized_result(result):
    """Count the complete result, including both representations when enabled."""
    return json.dumps(
        result.model_dump(mode="json", by_alias=True, exclude_none=True),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def token_counter(spec):
    """Load a bundled encoding or local tokenizer.json, with no network access."""
    if spec in _ENCODINGS:
        encoding = named_encoding(spec)
        return lambda text: len(encoding.encode(text, disallowed_special=()))
    path = Path(spec).expanduser()
    if not path.is_file():
        raise ValueError(
            "Tokenizer must be cl100k_base, o200k_base, or a local tokenizer.json file"
        )
    try:
        from tokenizers import Tokenizer
    except ImportError:
        raise ValueError("Install the tokens extra to use a local tokenizer") from None
    tokenizer = Tokenizer.from_file(str(path))
    return lambda text: len(tokenizer.encode(text, add_special_tokens=False).ids)


def validate_limit(max_tokens, counter):
    if max_tokens is None:
        return
    if type(max_tokens) is not int or not 1 <= max_tokens <= 131072:
        raise ValueError("max_tokens must be between 1 and 131072")
    if counter is None:
        raise ValueError("max_tokens requires a server configured with --tokenizer")
