"""Command-line entry points for the MCP server and local file operations."""

import argparse
import json
import sys
from pathlib import Path

from .settings import Settings
from .store import Workbook, default_root

TOOL_CHOICES = [
    "save",
    "find",
    "inspect",
    "resume",
    "query",
    "refresh",
    "import",
    "delete",
    "search",
    "describe",
    "diff",
]


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="workspool",
        description="Workspool: a local, versioned workbook for agent handoffs.",
    )
    parser.add_argument("--workspace", type=Path, default=default_root())
    bounds = parser.add_argument_group("limits")
    bounds.add_argument(
        "--index-bytes",
        type=int,
        default=Settings.index_bytes,
        help="bytes of each text artifact indexed for find (default: 65536)",
    )
    bounds.add_argument(
        "--query-timeout",
        type=float,
        default=Settings.query_timeout,
        help="seconds before a SELECT is interrupted (default: 15)",
    )
    bounds.add_argument(
        "--query-memory",
        default=Settings.query_memory,
        help="DuckDB memory limit, e.g. 512MB or 4GB (default: 1GB)",
    )
    bounds.add_argument(
        "--query-threads",
        type=int,
        default=Settings.query_threads,
        help="DuckDB threads per query (default: 2)",
    )
    bounds.add_argument(
        "--max-preview-rows",
        type=int,
        default=Settings.max_preview_rows,
        help="most rows a query preview may return (default: 25)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="Start the local MCP server")
    serve.add_argument(
        "--transport", choices=["stdio", "streamable-http"], default="stdio"
    )
    serve.add_argument("--port", type=int, default=8731)
    serve.add_argument(
        "--profile",
        choices=["compatible", "efficient"],
        default="compatible",
        help="efficient enables lazy discovery, adaptive replies, search, describe, and diff",
    )
    serve.add_argument(
        "--response-format",
        choices=["compact", "full", "text", "adaptive"],
        help="override profile format; adaptive chooses a lossless compact encoding",
    )
    serve.add_argument(
        "--response-content",
        choices=["both", "text", "structured"],
        default="both",
        help="return both MCP representations, or one copy for compatible hosts",
    )
    serve.add_argument(
        "--tool-discovery",
        choices=["eager", "lazy"],
        help="advertise all tool schemas, or discover them on demand through workspool",
    )
    serve.add_argument(
        "--tools",
        nargs="+",
        choices=TOOL_CHOICES,
        help=(
            "expose only these tools to this host (default: save find inspect "
            "resume query refresh; import and delete must be listed to be exposed)"
        ),
    )

    ingest = commands.add_parser(
        "import", help="Save a local file in its native format"
    )
    ingest.add_argument("file", type=Path)
    ingest.add_argument("--name")
    ingest.add_argument("--summary", default="")
    ingest.add_argument("--source", default="")
    ingest.add_argument("--expected-version", type=int)

    export = commands.add_parser(
        "export", help="Copy a version's original bytes to a new file"
    )
    export.add_argument("ref")
    export.add_argument("file", type=Path)

    for command in ("find", "resume"):
        lookup = commands.add_parser(command)
        lookup.add_argument("text", nargs="?", default="")
    inspect = commands.add_parser("inspect")
    inspect.add_argument("ref")
    inspect.add_argument("--excerpt-bytes", type=int, default=0)
    inspect.add_argument("--offset", type=int, default=0)
    inspect.add_argument("--include-recipe", action="store_true")

    delta = commands.add_parser(
        "diff", help="Read exact edits from a retained complete base version"
    )
    delta.add_argument("ref")
    delta.add_argument("--base", required=True)
    delta.add_argument("--max-bytes", type=int, default=8192)

    search = commands.add_parser("search", help="Find literal passages in source text")
    search.add_argument("text")
    search.add_argument("--ref", action="append")
    search.add_argument("--limit", type=int, default=5)
    search.add_argument("--context-bytes", type=int, default=256)
    search.add_argument("--cursor")

    describe = commands.add_parser(
        "describe", help="Read table columns and types without rows"
    )
    describe.add_argument("--input", action="append", required=True)
    describe.add_argument("--offset", type=int, default=0)
    describe.add_argument("--limit", type=int, default=50)
    describe.add_argument("--allow-historical", action="store_true")

    run = commands.add_parser(
        "query", help="Run one SELECT over CSV/Parquet artifacts as input1, input2, ..."
    )
    run.add_argument("sql")
    run.add_argument(
        "--input", action="append", required=True, help="artifact name or ID"
    )
    run.add_argument("--params", help="JSON list of scalar parameters", default="[]")
    run.add_argument("--limit", type=int, default=5)
    run.add_argument("--save-as")
    run.add_argument("--expected-version", type=int)
    run.add_argument("--allow-historical", action="store_true")

    remove = commands.add_parser(
        "delete",
        help="Delete one superseded version by ID, or every version of a name",
    )
    remove.add_argument("ref")
    remove.add_argument(
        "--expected-version", type=int, help="required when deleting by name"
    )

    prune = commands.add_parser(
        "prune", help="Drop superseded versions nothing pins and free their blobs"
    )
    prune.add_argument(
        "--keep", type=int, default=1, help="versions to keep per name (default: 1)"
    )
    prune.add_argument("--dry-run", action="store_true")

    bench = commands.add_parser("bench", help="Compare raw and referenced handoffs")
    bench.add_argument("--tokenizer", type=Path)
    args = parser.parse_args()

    try:
        if args.command == "bench":
            from .bench import benchmark

            result = benchmark(args.tokenizer)
        else:
            settings = Settings(
                index_bytes=args.index_bytes,
                query_timeout=args.query_timeout,
                query_memory=args.query_memory,
                query_threads=args.query_threads,
                max_preview_rows=args.max_preview_rows,
            )
            workbook = Workbook(args.workspace, settings)
            match args.command:
                case "serve":
                    from .server import server_for

                    server_for(
                        workbook,
                        args.port,
                        response_format=args.response_format,
                        response_content=args.response_content,
                        tool_discovery=args.tool_discovery,
                        tools=args.tools,
                        profile=args.profile,
                    ).run(transport=args.transport)
                    return
                case "import":
                    result = workbook.import_path(
                        args.file.resolve(),
                        name=args.name or "",
                        summary=args.summary,
                        source=args.source,
                        expected_version=args.expected_version,
                    )
                case "export":
                    workbook.export(args.ref, args.file)
                    return
                case "find":
                    result = workbook.find(args.text)
                case "resume":
                    result = workbook.resume(args.text)
                case "inspect":
                    result = workbook.inspect(
                        args.ref,
                        args.excerpt_bytes,
                        args.offset,
                        args.include_recipe,
                    )
                case "diff":
                    from .delta import diff

                    result = diff(workbook, args.ref, args.base, args.max_bytes)
                case "search":
                    from .retrieval import search

                    result = search(
                        workbook,
                        args.text,
                        args.ref,
                        args.limit,
                        args.context_bytes,
                        args.cursor,
                    )
                case "describe":
                    import duckdb

                    from .query import describe

                    try:
                        result = describe(
                            workbook,
                            args.input,
                            args.offset,
                            args.limit,
                            args.allow_historical,
                        )
                    except duckdb.Error as exc:
                        raise ValueError(exc) from None
                case "query":
                    import duckdb

                    from .query import query

                    parameters = json.loads(args.params)
                    if not isinstance(parameters, list):
                        raise TypeError("--params must be a JSON list")
                    try:
                        result = query(
                            workbook,
                            args.input,
                            args.sql,
                            parameters,
                            args.limit,
                            args.save_as,
                            args.expected_version,
                            args.allow_historical,
                        )
                    except duckdb.Error as exc:
                        raise ValueError(exc) from None
                case "delete":
                    result = workbook.delete(args.ref, args.expected_version)
                case "prune":
                    result = workbook.prune(args.keep, args.dry_run)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, TypeError, OSError, ImportError) as exc:
        print(f"{parser.prog}: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
