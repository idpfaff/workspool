"""Explicit, local token accounting for serialized MCP results."""

import json
from pathlib import Path


def serialized_result(result):
    """Count the complete result, including both representations when enabled."""
    return json.dumps(
        result.model_dump(mode="json", by_alias=True, exclude_none=True),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def token_counter(spec):
    """Load an explicit encoding or local tokenizer.json; never guess a model."""
    if spec in {"cl100k_base", "o200k_base"}:
        try:
            import tiktoken
        except ImportError:
            raise ValueError("Install the tokens extra to use --tokenizer") from None
        encoding = tiktoken.get_encoding(spec)
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
