"""Choose a compact, reversible text representation for an MCP result.

Selection minimizes serialized UTF-8 content bytes, including content-block
framing. This is a deterministic size heuristic, not an exact token estimate.
Only top-level artifact receipts are packed; nested user data stays unchanged.
"""

import copy
import json
from collections.abc import Sequence
from typing import Any

from mcp.types import TextContent

TAG = "workspool_encoding"
ENCODINGS = {"json-v1", "text-v1", "artifacts-v1", "sources-v1"}
INSTRUCTIONS = (
    "Adaptive replies may use workspool_encoding: fields/rows/defaults expand "
    "artifact metadata; excerpt_rows maps following exact text blocks."
)
SOURCE_INSTRUCTIONS = (
    "sources-v1 restores following text blocks at excerpt_paths in data."
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _blocks(metadata: Any, excerpts: Sequence[str] = ()) -> list[TextContent]:
    return [
        TextContent(type="text", text=_json(metadata)),
        *(TextContent(type="text", text=excerpt) for excerpt in excerpts),
    ]


def _is_envelope(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get(TAG), str)
        and value[TAG] in ENCODINGS
    )


def _artifacts(data: Any) -> tuple[list[dict], bool] | None:
    if not isinstance(data, dict):
        return None
    if "artifact_id" in data:
        return [data], True
    rows = data.get("artifacts")
    if (
        isinstance(rows, list)
        and rows
        and all(isinstance(row, dict) and "artifact_id" in row for row in rows)
    ):
        return rows, False
    return None


def _extract(rows: list[dict]) -> tuple[list[dict], list[int], list[str]]:
    metadata = []
    indexes = []
    excerpts = []
    for index, row in enumerate(rows):
        item = dict(row)
        if isinstance(item.get("excerpt"), str):
            indexes.append(index)
            excerpts.append(item.pop("excerpt"))
        metadata.append(item)
    return metadata, indexes, excerpts


def _excerpt_rows(indexes: list[int], count: int) -> str | list[int]:
    return "all" if len(indexes) == count else indexes


def _pack(rows: list[dict], *, data: dict, single: bool) -> dict:
    fields = list(dict.fromkeys(key for row in rows for key in row))
    defaults = {
        key: rows[0][key]
        for key in fields
        if key in rows[0]
        and all(key in row and _json(row[key]) == _json(rows[0][key]) for row in rows)
    }
    fields = [key for key in fields if key not in defaults]
    missing = [
        [row_index, field_index]
        for row_index, row in enumerate(rows)
        for field_index, field in enumerate(fields)
        if field not in row
    ]
    packed = {
        TAG: "artifacts-v1",
        "fields": fields,
        "rows": [[row.get(field) for field in fields] for row in rows],
    }
    if defaults:
        packed["defaults"] = defaults
    if missing:
        packed["missing"] = missing
    if single:
        packed["single"] = True
    elif len(data) > 1:
        packed["extra"] = {
            key: value for key, value in data.items() if key != "artifacts"
        }
    return packed


def content_bytes(content: Sequence[TextContent]) -> int:
    """Bytes of the JSON content array, including escaped text and block headers."""
    return len(
        _json(
            [block.model_dump(by_alias=True, exclude_none=True) for block in content]
        ).encode("utf-8")
    )


def _source_blocks(data):
    """Only extract the defined sync and retrieve fields, never arbitrary user data."""
    if not isinstance(data, dict) or _is_envelope(data):
        return None
    paths = []
    if (
        data.get("mode") == "full"
        and isinstance(data.get("artifact"), dict)
        and "artifact_id" in data["artifact"]
        and isinstance(data.get("excerpt"), str)
    ):
        paths.append(["excerpt"])
    if isinstance(data.get("spans"), list) and isinstance(data.get("artifacts"), list):
        paths.extend(
            ["spans", index, "excerpt"]
            for index, span in enumerate(data["spans"])
            if isinstance(span, dict) and isinstance(span.get("excerpt"), str)
        )
    if not paths:
        return None
    metadata = copy.deepcopy(data)
    excerpts = []
    for path in paths:
        target = metadata
        for key in path[:-1]:
            target = target[key]
        excerpts.append(target.pop(path[-1]))
    return _blocks(
        {TAG: "sources-v1", "data": metadata, "excerpt_paths": paths}, excerpts
    )


def render(data: Any, *, measure=None) -> list[TextContent]:
    """Return the smallest supported representation without changing its data.

    Ordinary compact JSON stays unwrapped unless it collides with a reserved
    adaptive envelope. Ties favor the simpler candidate. The result is always
    reversible by :func:`decode`, including missing keys and empty excerpts.
    """
    plain = {TAG: "json-v1", "data": data} if _is_envelope(data) else data
    candidates = [_blocks(plain)]
    sources = _source_blocks(data)
    if sources is not None:
        candidates.append(sources)
    measure = content_bytes if measure is None else measure
    artifact_set = _artifacts(data)
    if artifact_set is None:
        return min(candidates, key=measure)
    rows, single = artifact_set
    metadata, indexes, excerpts = _extract(rows)
    candidates.append(_blocks(_pack(rows, data=data, single=single)))
    if excerpts:
        raw_data = metadata[0] if single else {**data, "artifacts": metadata}
        raw = {
            TAG: "text-v1",
            "data": raw_data,
            "excerpt_rows": _excerpt_rows(indexes, len(rows)),
        }
        if single:
            raw["single"] = True
        candidates.append(_blocks(raw, excerpts))
        packed = _pack(metadata, data=data, single=single)
        packed["excerpt_rows"] = _excerpt_rows(indexes, len(rows))
        candidates.append(_blocks(packed, excerpts))
    return min(candidates, key=measure)


def decode(content: Sequence[TextContent]) -> Any:
    """Reconstruct adaptive text content without consulting structuredContent.

    ``artifacts-v1`` stores varying fields once in ``fields`` and their values
    in row arrays. ``defaults`` supplies fields common to all rows. ``missing``
    lists zero-based [row, field] pairs whose null placeholders mean absence.
    ``single`` changes the result from {"artifacts": rows} to the one artifact;
    ``extra`` preserves other top-level batch keys. ``excerpt_rows`` names the
    rows receiving content blocks 1 onward, or is "all" for every row.
    ``text-v1`` has ordinary ``data`` metadata plus the same excerpt mapping.
    ``json-v1`` escapes a user value that resembles a reserved envelope.
    """
    if not content or any(block.type != "text" for block in content):
        raise ValueError("Adaptive content must contain text blocks")
    envelope = json.loads(content[0].text)
    if not _is_envelope(envelope):
        if len(content) != 1:
            raise ValueError("Unexpected blocks after compact JSON")
        return envelope
    encoding = envelope[TAG]
    if encoding == "json-v1":
        if len(content) != 1:
            raise ValueError("Unexpected blocks after escaped JSON")
        return envelope["data"]
    if encoding == "sources-v1":
        data = envelope["data"]
        paths = envelope.get("excerpt_paths")
        if not isinstance(paths, list) or len(paths) != len(content) - 1:
            raise ValueError("Source paths do not match content blocks")
        seen = set()
        for path, block in zip(paths, content[1:], strict=True):
            valid = path == ["excerpt"] or (
                isinstance(path, list)
                and len(path) == 3
                and path[0] == "spans"
                and type(path[1]) is int
                and path[1] >= 0
                and path[2] == "excerpt"
            )
            if not valid or tuple(path) in seen:
                raise ValueError("Invalid or duplicate source excerpt path")
            seen.add(tuple(path))
            try:
                target = data
                for key in path[:-1]:
                    target = target[key]
                if not isinstance(target, dict) or path[-1] in target:
                    raise ValueError("Source excerpt path overwrites existing data")
                target[path[-1]] = block.text
            except (KeyError, IndexError, TypeError):
                raise ValueError("Invalid source excerpt path") from None
        return data
    single = envelope.get("single", False)
    if encoding == "text-v1":
        data = envelope["data"]
        rows = [data] if single else data["artifacts"]
    else:
        fields = envelope["fields"]
        values = envelope["rows"]
        if len(set(fields)) != len(fields) or any(
            len(row) != len(fields) for row in values
        ):
            raise ValueError("Invalid packed artifact fields or row widths")
        rows = [
            copy.deepcopy(envelope.get("defaults", {}))
            | dict(zip(fields, row, strict=True))
            for row in values
        ]
        for row_index, field_index in envelope.get("missing", []):
            if not 0 <= row_index < len(rows) or not 0 <= field_index < len(fields):
                raise ValueError("Invalid missing field index")
            del rows[row_index][fields[field_index]]
        if single:
            if len(rows) != 1:
                raise ValueError("A single artifact requires one row")
            data = rows[0]
        else:
            data = {**envelope.get("extra", {}), "artifacts": rows}
    indexes = envelope.get("excerpt_rows", [])
    if indexes == "all":
        indexes = list(range(len(rows)))
    if not isinstance(indexes, list) or len(indexes) != len(content) - 1:
        raise ValueError("Excerpt mapping does not match content blocks")
    if any(type(index) is not int or not 0 <= index < len(rows) for index in indexes):
        raise ValueError("Invalid excerpt row index")
    if len(set(indexes)) != len(indexes):
        raise ValueError("Duplicate excerpt row index")
    for index, block in zip(indexes, content[1:], strict=True):
        rows[index]["excerpt"] = block.text
    return data
