"""Download provenance must describe final bytes, including buffered small files."""

import bz2
import hashlib
import importlib.util
import io
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "public_data", Path(__file__).parents[1] / "benchmarks/public_data.py"
)
bench = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bench)


class Response(io.BytesIO):
    def __init__(self, body, length=None):
        super().__init__(body)
        self.headers = {"Content-Length": str(len(body) if length is None else length)}


def test_small_download_hashes_flushed_bytes_and_reuses_verified_file(
    tmp_path, monkeypatch
):
    body = b"A short official data dictionary.\n"
    calls = []

    def open_url(*args, **kwargs):
        calls.append(1)
        return Response(body)

    monkeypatch.setattr(bench.urllib.request, "urlopen", open_url)
    args = ("https://example.invalid/readme", tmp_path / "readme.txt")
    first = bench.fetch(args)
    assert first["sha256"] == hashlib.sha256(body).hexdigest()
    assert first["bytes"] == len(body) and args[1].read_bytes() == body
    assert bench.fetch(args) == first and len(calls) == 1
    args[1].write_bytes(b"modified")
    with pytest.raises(ValueError, match="changed"):
        bench.fetch(args)


def test_partial_download_cannot_publish_success_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(
        bench.urllib.request, "urlopen", lambda *a, **k: Response(b"short", 100)
    )
    with pytest.raises(ValueError, match="Incomplete"):
        bench.fetch(("https://example.invalid/data", tmp_path / "data.gz"))
    assert not (tmp_path / "data.gz").exists()
    assert not (tmp_path / "data.gz.metadata.json").exists()


def test_wikipedia_selection_preserves_revision_text_and_provenance(tmp_path):
    root = tmp_path / "wikipedia"
    root.mkdir()
    xml = b"""<mediawiki xmlns="http://www.mediawiki.org/xml/export-0.11/">
<page><title>Redirect</title><ns>0</ns><id>1</id><redirect title="Target"/><revision><id>10</id><text>skip</text></revision></page>
<page><title>Target</title><ns>0</ns><id>2</id><revision><id>20</id><text>Exact &amp; Unicode: \xc3\xa9\n</text></revision></page>
<page><title>Talk</title><ns>1</ns><id>3</id><revision><id>30</id><text>skip</text></revision></page>
</mediawiki>"""
    archive = root / bench.WIKI_FILE
    archive.write_bytes(bz2.compress(xml))
    bench.wikipedia(tmp_path, limit=10)
    manifest = json.loads((root / "documents/manifest.json").read_text())
    assert len(manifest["documents"]) == 1
    row = manifest["documents"][0]
    body = (root / "documents" / row["path"]).read_bytes()
    assert body == "Exact & Unicode: é\n".encode()
    assert row["sha256"] == hashlib.sha256(body).hexdigest()
    assert row["url"] == "https://en.wikipedia.org/w/index.php?oldid=20"
