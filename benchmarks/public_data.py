"""Download and stream-prepare public stress inputs; raw records remain local."""

import argparse
import bz2
import hashlib
import json
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

WIKI = "https://dumps.wikimedia.org/enwiki/20261001/"
WIKI_FILE = "enwiki-20261001-pages-articles-multistream1.xml-p1p41242.bz2"
NOAA = "https://www.ncei.noaa.gov/pub/data/ghcn/daily/by_year/"
TAXI = "https://d37ci6vzurychx.cloudfront.net/trip-data/"


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def digest(path, algorithm="sha256"):
    result = hashlib.new(algorithm)
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def fetch(item):
    url, path = item
    manifest = path.with_suffix(path.suffix + ".metadata.json")
    if path.exists() and manifest.exists():
        saved = json.loads(manifest.read_text())
        if saved["sha256"] == digest(path):
            return saved
        raise ValueError("Existing public input has changed; use a fresh directory")
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".partial")
    started = time.monotonic()
    for attempt in range(3):
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "Workspool-public-stress/0.7"}
            )
            with (
                urllib.request.urlopen(request, timeout=60) as response,
                partial.open("wb") as output,
            ):
                size = 0
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    size += len(chunk)
                    if size > 1024**3:
                        raise ValueError(
                            "Public input exceeded the 1 GiB per-file download cap"
                        )
                    output.write(chunk)
                advertised = response.headers.get("Content-Length")
                if advertised is not None and size != int(advertised):
                    raise ValueError("Incomplete public download")
                output.flush()
                metadata = {
                    "url": url,
                    "file": path.name,
                    "bytes": size,
                    "etag": response.headers.get("ETag"),
                    "last_modified": response.headers.get("Last-Modified"),
                    "retrieved_at": datetime.now(UTC).isoformat(),
                    "sha256": digest(partial),
                    "download_seconds": time.monotonic() - started,
                }
            partial.replace(path)
            write_json(manifest, metadata)
            print(json.dumps({"downloaded": path.name, "bytes": size}), flush=True)
            return metadata
        except OSError:
            if attempt == 2:
                raise
            time.sleep(1 + attempt)
    raise RuntimeError("Download did not complete")


def download(root):
    jobs = [
        (
            TAXI + f"yellow_tripdata_2025-{month:02d}.parquet",
            root / "taxi" / f"yellow_tripdata_2025-{month:02d}.parquet",
        )
        for month in range(1, 13)
    ]
    jobs += [
        (NOAA + "2024.csv.gz", root / "noaa" / "2024.csv.gz"),
        (NOAA + "readme-by_year.txt", root / "noaa" / "readme-by_year.txt"),
        (WIKI + WIKI_FILE, root / "wikipedia" / WIKI_FILE),
        (WIKI + "enwiki-20261001-sha1sums.txt", root / "wikipedia" / "sha1sums.txt"),
    ]
    with ThreadPoolExecutor(max_workers=3) as pool:
        records = list(pool.map(fetch, jobs))
    checksums = (root / "wikipedia/sha1sums.txt").read_text()
    expected = next(
        line.split()[0]
        for line in checksums.splitlines()
        if line.split()[-1] == WIKI_FILE
    )
    assert re.fullmatch(r"[0-9a-f]{40}", expected)
    if digest(root / "wikipedia" / WIKI_FILE, "sha1") != expected:
        raise ValueError("Wikipedia archive failed its official SHA-1 checksum")
    write_json(
        root / "downloads.json", {"files": records, "wikipedia_official_sha1": expected}
    )


def noaa(root):
    import pyarrow as pa
    import pyarrow.parquet as pq
    from pyarrow import csv

    directory = root / "noaa/prepared"
    directory.mkdir(exist_ok=True)
    if (directory / "manifest.json").exists():
        return
    names = ["station", "date", "element", "value", "mflag", "qflag", "sflag", "time"]
    types = {
        name: pa.int32() if name in {"date", "value"} else pa.string() for name in names
    }
    rows, files, writer, in_file = 0, [], None, 0
    with pa.input_stream(root / "noaa/2024.csv.gz", compression="gzip") as stream:
        batches = csv.open_csv(
            stream,
            read_options=csv.ReadOptions(column_names=names, block_size=8 * 1024**2),
            convert_options=csv.ConvertOptions(
                column_types=types, strings_can_be_null=False
            ),
        )
        try:
            for batch in batches:
                if writer is None:
                    target = directory / f"part-{len(files):03d}.parquet"
                    writer = pq.ParquetWriter(target, batch.schema, compression="zstd")
                    files.append(target)
                    in_file = 0
                writer.write_batch(batch)
                rows += batch.num_rows
                in_file += batch.num_rows
                if in_file >= 2_000_000:
                    writer.close()
                    writer = None
        finally:
            if writer:
                writer.close()
    write_json(
        directory / "manifest.json",
        {
            "rows": rows,
            "files": [
                {
                    "path": path.name,
                    "bytes": path.stat().st_size,
                    "sha256": digest(path),
                }
                for path in files
            ],
            "source_sha256": digest(root / "noaa/2024.csv.gz"),
            "schema": {key: str(value) for key, value in types.items()},
            "quality_policy": "All rows and flags preserved; sums stay grouped by element, never interpreted across units.",
        },
    )
    print(
        json.dumps({"prepared": "noaa", "rows": rows, "files": len(files)}), flush=True
    )


def wikipedia(root, limit=8192):
    directory = root / "wikipedia/documents"
    directory.mkdir(exist_ok=True)
    if (directory / "manifest.json").exists():
        return
    archive = root / "wikipedia" / WIKI_FILE
    records = []
    ns = {"m": "http://www.mediawiki.org/xml/export-0.11/"}
    with bz2.open(archive, "rb") as source:
        for _, page in ET.iterparse(source, events=("end",)):
            if page.tag.rsplit("}", 1)[-1] != "page":
                continue
            namespace = page.tag.split("}")[0].lstrip("{")
            ns["m"] = namespace
            revision = page.find("m:revision", ns)
            if (
                page.findtext("m:ns", namespaces=ns) == "0"
                and page.find("m:redirect", ns) is None
                and revision is not None
            ):
                body = revision.findtext("m:text", default="", namespaces=ns)
                if body.strip():
                    page_id = page.findtext("m:id", namespaces=ns)
                    revision_id = revision.findtext("m:id", namespaces=ns)
                    target = directory / f"page-{page_id}.txt"
                    raw = body.encode("utf-8")
                    target.write_bytes(raw)
                    records.append(
                        {
                            "path": target.name,
                            "title": page.findtext("m:title", namespaces=ns),
                            "page_id": page_id,
                            "revision_id": revision_id,
                            "url": "https://en.wikipedia.org/w/index.php?oldid="
                            + revision_id,
                            "sha256": hashlib.sha256(raw).hexdigest(),
                            "bytes": len(raw),
                        }
                    )
            page.clear()
            if len(records) >= limit:
                break
    write_json(
        directory / "manifest.json",
        {
            "documents": records,
            "source_archive": {
                "url": WIKI + WIKI_FILE,
                "sha256": digest(archive),
                "selection": f"First {limit} nonempty, namespace-0, nonredirect pages in archive order",
                "format": "UTF-8 wikitext extracted from revision text",
            },
            "total_bytes": sum(item["bytes"] for item in records),
        },
    )
    print(
        json.dumps(
            {
                "prepared": "wikipedia",
                "documents": len(records),
                "bytes": sum(item["bytes"] for item in records),
            }
        ),
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--mode", choices=["download", "prepare", "all"], default="all")
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=True)
    if args.mode in {"download", "all"}:
        download(args.root)
    if args.mode in {"prepare", "all"}:
        noaa(args.root)
        wikipedia(args.root)


if __name__ == "__main__":
    main()
