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
    "sync",
    "retrieve",
    "task_define",
    "task_run",
    "task_status",
    "context",
    "recall",
    "remember",
    "session_status",
    "claim",
    "claim_renew",
    "claim_release",
    "claim_owner",
    "provenance",
    "changes",
    "approve",
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
        choices=["compatible", "efficient", "agent", "runtime"],
        default="compatible",
        help="agent adds durable tasks and indexed context with text-only replies; efficient enables retrieval and synchronization",
    )
    serve.add_argument(
        "--response-format",
        choices=["compact", "full", "text", "adaptive"],
        help="override profile format; adaptive chooses a lossless compact encoding",
    )
    serve.add_argument(
        "--response-content",
        choices=["both", "text", "structured"],
        default=None,
        help="return both MCP representations, or one copy for compatible hosts",
    )
    serve.add_argument(
        "--tool-discovery",
        choices=["eager", "lazy"],
        help="advertise all tool schemas, or discover them on demand through workspool",
    )
    serve.add_argument(
        "--tokenizer",
        help="cl100k_base, o200k_base, or local tokenizer.json; enables exact max_tokens budgets",
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
    serve.add_argument(
        "--agent-id",
        default="local",
        help="trusted identity for this server; never taken from tool input",
    )
    serve.add_argument(
        "--role", choices=["reader", "writer", "approver"], default="writer"
    )
    serve.add_argument(
        "--goal",
        help="restore relevant context automatically on startup; requires --tokenizer",
    )
    serve.add_argument("--recall-tokens", type=int, default=1500)
    serve.add_argument(
        "--model-dir",
        type=Path,
        help="provisioned local embedding model; omitted means lexical recall",
    )
    serve.add_argument(
        "--require-claims",
        action="store_true",
        help="persistently require fenced task claims in this workspace",
    )

    model = commands.add_parser(
        "model-download",
        help="Explicitly download and pin the optional local embedding model",
    )
    model.add_argument("directory", type=Path)
    hook = commands.add_parser(
        "hook",
        help="Host integration: session lifecycle and explicit JSON events on stdin",
    )
    hook.add_argument(
        "kind", choices=["session_start", "decision", "tool_result", "session_end"]
    )
    hook.add_argument("--agent-id", default="local")
    hook.add_argument("--session-id")
    hook.add_argument("--event-id")
    hook.add_argument("--goal", default="Workspace activity")

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

    sync = commands.add_parser(
        "sync", help="Read unchanged, delta, or full text in one call"
    )
    sync.add_argument("ref")
    sync.add_argument(
        "--base",
        type=lambda value: int(value) if value.isdecimal() else value,
        help="retained complete version number or immutable artifact ID",
    )
    sync.add_argument("--offset", type=int, default=0)
    sync.add_argument("--max-bytes", type=int, default=8192)
    retrieve = commands.add_parser(
        "retrieve", help="Merge literal searches and independent reads"
    )
    retrieve.add_argument(
        "--queries", default="[]", help="JSON list of search requests"
    )
    retrieve.add_argument(
        "--reads", default="[]", help="JSON list of ref/offset/bytes requests"
    )
    retrieve.add_argument("--max-bytes", type=int, default=32768)
    define = commands.add_parser(
        "task-define", help="Save a reusable source, SQL-step and evidence plan"
    )
    define.add_argument("name")
    define.add_argument("file", type=Path, help="JSON task definition")
    define.add_argument("--expected-version", type=int)
    task_run = commands.add_parser(
        "task-run", help="Reconcile a task against pinned current inputs"
    )
    task_run.add_argument("name")
    task_run.add_argument("--resume-from")
    task_run.add_argument("--force", action="store_true")
    task_status = commands.add_parser(
        "task-status", help="Reopen a task run without recomputing"
    )
    task_status.add_argument("name")
    task_status.add_argument("--run-id")
    for task_command in (task_run, task_status):
        task_command.add_argument(
            "--base-run", help="Only when retaining the prior unabridged brief"
        )
    context = commands.add_parser(
        "context", help="Rank exact passages using the persistent full-source index"
    )
    context.add_argument("query")
    context.add_argument("--ref", action="append")
    context.add_argument("--limit", type=int, default=8)
    recall = commands.add_parser(
        "recall", help="Budgeted local hybrid recall with exact source citations"
    )
    recall.add_argument("query")
    recall.add_argument("--ref", action="append")
    recall.add_argument("--limit", type=int, default=6)
    recall.add_argument("--model-dir", type=Path)
    recall.add_argument("--max-bytes", type=int, default=16384)
    for bounded in (task_run, task_status, context):
        bounded.add_argument("--max-bytes", type=int, default=16384)
    for bounded in (sync, retrieve, task_run, task_status, context, recall):
        bounded.add_argument("--max-tokens", type=int)
        bounded.add_argument(
            "--tokenizer", help="encoding name or local tokenizer.json"
        )

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
        if args.command == "model-download":
            from .semantic import provision_model

            result = provision_model(args.directory)
        elif args.command == "bench":
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
            if args.command in {
                "sync",
                "retrieve",
                "task-run",
                "task-status",
                "context",
                "recall",
            }:
                from .budget import token_counter, validate_limit

                counter = token_counter(args.tokenizer) if args.tokenizer else None
                validate_limit(args.max_tokens, counter)
                cost = (
                    (
                        lambda data: counter(
                            json.dumps(data, ensure_ascii=False, indent=2) + "\n"
                        )
                    )
                    if counter
                    else None
                )
                fits = (
                    (lambda data: cost(data) <= args.max_tokens)
                    if args.max_tokens is not None
                    else None
                )
            match args.command:
                case "serve":
                    from .server import server_for

                    app = server_for(
                        workbook,
                        args.port,
                        response_format=args.response_format,
                        response_content=args.response_content,
                        tool_discovery=args.tool_discovery,
                        tools=args.tools,
                        profile=args.profile,
                        tokenizer=args.tokenizer,
                        agent_id=args.agent_id,
                        role=args.role,
                        goal=args.goal,
                        recall_tokens=args.recall_tokens,
                        model_dir=args.model_dir,
                        require_claims=args.require_claims,
                    )
                    try:
                        app.run(transport=args.transport)
                    finally:
                        app.finish_session()
                    return
                case "hook":
                    from .continuity import Session, capture_event, start_session
                    from .coordination import Principal

                    principal = Principal(args.agent_id)
                    if args.kind == "session_start":
                        result = start_session(
                            workbook, principal, args.goal, args.session_id
                        ).as_dict()
                    else:
                        if not args.session_id:
                            raise ValueError("This hook requires --session-id")
                        raw = sys.stdin.buffer.read(16385)
                        if len(raw) > 16384:
                            raise ValueError("Hook event exceeds 16 KiB")
                        data = json.loads(raw or b"{}")
                        if not isinstance(data, dict):
                            raise ValueError("Hook event must be a JSON object")
                        result = capture_event(
                            Session(workbook, principal, args.session_id),
                            args.kind,
                            data,
                            event_id=args.event_id,
                        )
                case "recall":
                    from .semantic import hybrid_search

                    result = hybrid_search(
                        workbook,
                        args.query,
                        args.ref,
                        model_dir=args.model_dir,
                        limit=args.limit,
                        max_bytes=args.max_bytes,
                        fits=fits,
                    )
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
                case "sync":
                    from .sync import sync

                    result = sync(
                        workbook,
                        args.ref,
                        args.base,
                        args.offset,
                        args.max_bytes,
                        fits=fits,
                        cost=cost,
                    )
                case "retrieve":
                    from .batch_retrieval import retrieve

                    result = retrieve(
                        workbook,
                        json.loads(args.queries),
                        json.loads(args.reads),
                        args.max_bytes,
                        fits=fits,
                    )
                case "task-define":
                    from .tasks import define_task

                    with args.file.open("rb") as stream:
                        raw = stream.read(32769)
                    if len(raw) > 32768:
                        raise ValueError("Task definition exceeds 32 KiB")
                    definition = json.loads(raw)
                    if not isinstance(definition, dict):
                        raise TypeError("Task definition must be a JSON object")
                    result = define_task(
                        workbook,
                        args.name,
                        **definition,
                        expected_version=args.expected_version,
                    )
                case "task-run":
                    from .tasks import run_task

                    result = run_task(
                        workbook,
                        args.name,
                        args.resume_from,
                        args.force,
                        args.max_bytes,
                        fits,
                        args.base_run,
                    )
                case "task-status":
                    from .tasks import status

                    result = status(
                        workbook,
                        args.name,
                        args.run_id,
                        args.max_bytes,
                        fits,
                        args.base_run,
                    )
                case "context":
                    from .context_index import ranked_search

                    result = ranked_search(
                        workbook,
                        args.query,
                        args.ref,
                        limit=args.limit,
                        max_bytes=args.max_bytes,
                    )
                    if fits is not None:
                        while result["hits"] and not fits(result):
                            result["hits"].pop()
                            result["complete"], result["truncated"] = False, True
                        if (
                            not fits(result)
                            or not result["hits"]
                            and result["truncated"]
                        ):
                            raise ValueError(
                                "Context token budget cannot fit one exact chunk and its provenance"
                            )
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
