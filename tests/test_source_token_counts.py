"""Prove stream counts at CSV seams without downloading tokenizer rank files."""

import hashlib
import importlib.util
import io
from pathlib import Path

import pytest

tiktoken = pytest.importorskip("tiktoken")
public = pytest.importorskip("tiktoken_ext.openai_public")
SPEC = importlib.util.spec_from_file_location(
    "source_token_counts",
    Path(__file__).parents[1] / "benchmarks/count_source_tokens.py",
)
bench = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bench)


@pytest.fixture
def encoders(monkeypatch):
    # Cross-newline tokens would merge if the regex allowed a spanning pre-token.
    ranks = {bytes([index]): index for index in range(256)}
    for value in (b"\nA", b"1\nA", b"AA", b"AAP", b"AAPL", b"12", b"123"):
        ranks[value] = len(ranks)
    monkeypatch.setattr(public, "load_tiktoken_bpe", lambda *args, **kwargs: ranks)
    return {
        name: tiktoken.Encoding(**getattr(public, name)()) for name in bench.ENCODINGS
    }


@pytest.mark.parametrize("chunk_bytes", [32, 43, 64, 99])
def test_measure_matches_whole_encoding_and_hashes(
    tmp_path, monkeypatch, encoders, chunk_bytes
):
    source = (
        b"ticker,t,v\nAAPL,1720698960000,913\nBRK.B,1720699020000,0\n"
        b"msft,1720699080000,1001\nX,1720699140000,-5\n"
    )
    path = tmp_path / "bars.csv"
    path.write_bytes(source)
    monkeypatch.setattr(bench, "load_encoders", lambda: encoders)
    result = bench.measure(path, workers=2, chunk_bytes=chunk_bytes, prefix_rows=2)
    assert result["source"]["tokens"] == bench.token_counts(source, encoders)
    assert result["source"]["sha256"] == hashlib.sha256(source).hexdigest()
    assert result["source"]["rows"] == 4 and result["source"]["headers"] == 1
    assert result["source"]["bytes"] == len(source)
    prefix = b"".join(source.splitlines(keepends=True)[:3])
    assert result["prefix"]["tokens"] == bench.token_counts(prefix, encoders)
    assert result["prefix"]["sha256"] == hashlib.sha256(prefix).hexdigest()
    assert result["prefix"]["rows"] == 2
    assert result["method"]["patterns"] == {
        name: codec._pat_str for name, codec in encoders.items()
    }


def test_adversarial_cross_newline_rank_is_blocked_by_both_actual_patterns(encoders):
    source = b"AAPL,1\nAAPL,2\n"
    blocks = list(bench.chunks(io.BytesIO(source), chunk_bytes=7))
    assert b"".join(blocks) == source and len(blocks) == 2
    for codec in encoders.values():
        whole = codec.encode_ordinary(source.decode())
        split = [
            token for block in blocks for token in codec.encode_ordinary(block.decode())
        ]
        assert split == whole
        # Negative control proves these ranks detect a cross-boundary merge.
        unsafe = tiktoken.Encoding(
            name="unsafe",
            pat_str=r"(?s:.+)",
            mergeable_ranks=codec._mergeable_ranks,
            special_tokens={},
        )
        assert len(unsafe.encode_ordinary(source.decode())) != sum(
            len(unsafe.encode_ordinary(block.decode())) for block in blocks
        )


@pytest.mark.parametrize(
    "source",
    [
        b"A,1\r\n",
        b'A,"1"\n',
        b"A,1\n\nB,2\n",
        b"1ABC,2\n",
        b"A,not_numeric\n",
        b"A,1",
        "A,é1\n".encode(),
        b"A," + b"1" * 5000 + b"\n",
    ],
)
def test_outside_proven_contract_is_rejected(source):
    with pytest.raises(ValueError, match="contract"):
        list(bench.chunks(io.BytesIO(source), chunk_bytes=32))


def test_unknown_regex_is_rejected_and_cache_hook_restored(monkeypatch, encoders):
    import tiktoken.load

    original = tiktoken.load.read_file
    unsafe = tiktoken.Encoding(
        name="cl100k_base",
        pat_str=r"(?s:.+)",
        mergeable_ranks=encoders["cl100k_base"]._mergeable_ranks,
        special_tokens={},
    )
    bench.validate_encoders(encoders)
    with pytest.raises(ValueError, match="(?i)pattern"):
        bench.validate_encoders(
            {"cl100k_base": unsafe, "o200k_base": encoders["o200k_base"]}
        )
    monkeypatch.setattr(tiktoken, "get_encoding", lambda name: unsafe)
    with pytest.raises(ValueError, match="(?i)pattern"):
        bench.load_encoders()
    assert tiktoken.load.read_file is original
