"""Bounded SQL over explicit, versioned workbook inputs."""

import json
import math
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import BoundedSemaphore, Timer

import duckdb

from .store import Workbook

QUERY_SLOT = BoundedSemaphore(1)
PARQUET = "application/vnd.apache.parquet"
MAX_SAFE_JSON_INTEGER = (1 << 53) - 1
Scalar = str | int | float | bool | None


def _inputs(workbook, inputs, allow_historical):
    if not 1 <= len(inputs) <= 8 or len(set(inputs)) != len(inputs):
        raise ValueError("Provide 1–8 distinct input references")
    tables = workbook.table_inputs(inputs, allow_historical)
    if len({item["artifact_id"] for item, _ in tables}) != len(tables):
        raise ValueError("Input references must resolve to distinct versions")
    return tables


@contextmanager
def _connection(workbook, tables, output=None):
    """The same restricted file access for queries and schema discovery."""
    settings = workbook.settings
    with duckdb.connect(
        config={
            "threads": str(settings.query_threads),
            "memory_limit": settings.query_memory,
            "autoload_known_extensions": "false",
            "autoinstall_known_extensions": "false",
        }
    ) as db:
        paths = [str(path) for _, path in tables]
        if output is not None:
            paths.append(str(output))
        db.execute("SET allowed_paths = [" + ", ".join(map(literal, paths)) + "]")
        db.execute("SET enable_external_access = false")
        db.execute("SET lock_configuration = true")
        for index, (item, path) in enumerate(tables, 1):
            reader = (
                "read_parquet" if item["media_type"] == PARQUET else "read_csv_auto"
            )
            db.execute(
                f"CREATE VIEW input{index} AS SELECT * FROM {reader}({literal(str(path))})"
            )
        yield db


def describe(workbook, inputs, offset=0, limit=50, allow_historical=False):
    """Return exact column names/types, without rows or unrequested scans for counts."""
    if not isinstance(offset, int) or offset < 0:
        raise ValueError("Offset must be a non-negative column index")
    if not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("Describe limit must be between 1 and 100 columns per table")
    tables = _inputs(workbook, inputs, allow_historical)
    result = []
    with QUERY_SLOT, _connection(workbook, tables) as db:
        deadline = Timer(workbook.settings.query_timeout, db.interrupt)
        deadline.start()
        try:
            for index, (item, _) in enumerate(tables, 1):
                relation = db.sql(f"SELECT * FROM input{index} LIMIT 0")
                columns, types = relation.columns, relation.types
                page = []
                budget = 7500
                for name, sql_type in zip(
                    columns[offset : offset + limit],
                    types[offset : offset + limit],
                    strict=True,
                ):
                    column = [name, str(sql_type)]
                    size = len(json.dumps(column, ensure_ascii=False).encode())
                    if size > 7500:
                        raise ValueError(
                            "A column name/type exceeds the schema page budget"
                        )
                    if size > budget:
                        break
                    budget -= size
                    page.append(column)
                end = min(offset + len(page), len(columns))
                result.append(
                    {
                        **{
                            key: item[key]
                            for key in (
                                "artifact_id",
                                "name",
                                "version",
                                "is_latest",
                                "stale_inputs",
                            )
                        },
                        "input": f"input{index}",
                        "columns": [column[0] for column in page],
                        "types": [column[1] for column in page],
                        "column_count": len(columns),
                        "next_offset": end if end < len(columns) else None,
                    }
                )
        finally:
            deadline.cancel()
            deadline.join()
    return {"tables": result}


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _preview(cursor, limit: int) -> dict:
    description = cursor.description
    columns = [col[0] for col in description]
    rows = []
    integer_strings = set()
    truncated = len(columns) > 24
    columns = columns[:24]
    clipped_columns = [name[:120] for name in columns]
    truncated |= columns != clipped_columns
    columns = clipped_columns
    budget = 7500 - len(json.dumps(columns, ensure_ascii=False).encode())
    for index, row in enumerate(cursor.fetchmany(limit + 1)):
        if index == limit:
            truncated = True
            break
        cells = []
        string_columns = set()
        for column, value in enumerate(row[:24]):
            if type(value) is int and abs(value) > MAX_SAFE_JSON_INTEGER:
                value = str(value)
                string_columns.add(column)
            if isinstance(value, float) and not math.isfinite(value):
                value = str(value)
            if value is not None and not isinstance(value, (str, int, float, bool)):
                value = str(value)
            if isinstance(value, str) and len(value) > 240:
                value = value[:239] + "…"
                truncated = True
            cells.append(value)
        size = len(json.dumps(cells, ensure_ascii=False).encode()) + 2
        if size > budget:
            truncated = True
            break
        budget -= size
        rows.append(cells)
        integer_strings.update(string_columns)
    result = {"columns": columns, "rows": rows, "preview_truncated": truncated}
    if integer_strings:
        result["integer_string_columns"] = [
            {"index": index, "sql_type": str(description[index][1])}
            for index in sorted(integer_strings)
        ]
    return result


def query(
    workbook: Workbook,
    inputs: list[str],
    sql: str,
    parameters: list[Scalar] | None = None,
    limit: int = 5,
    save_as: str | None = None,
    expected_version: int | None = None,
    allow_historical: bool = False,
) -> dict:
    if not sql.strip() or len(sql) > 6000 or len(parameters or []) > 30:
        raise ValueError(
            "Provide a SELECT of at most 6000 characters and at most 30 parameters"
        )
    if any(isinstance(p, str) and len(p) > 1000 for p in parameters or []):
        raise ValueError("String parameters must be at most 1000 characters")
    tables = _inputs(workbook, inputs, allow_historical)
    refs = [item["artifact_id"] for item, _ in tables]
    settings = workbook.settings
    rows = max(1, min(limit, settings.max_preview_rows))

    with QUERY_SLOT, TemporaryDirectory(dir=workbook.root) as scratch:
        output = Path(scratch) / "result.parquet"
        with _connection(workbook, tables, output if save_as else None) as db:
            statements = db.extract_statements(sql)
            if (
                len(statements) != 1
                or statements[0].type != duckdb.StatementType.SELECT
            ):
                raise ValueError("Only one SELECT statement is allowed")
            deadline = Timer(settings.query_timeout, db.interrupt)
            deadline.start()
            try:
                if save_as:
                    db.sql(sql, params=parameters or []).write_parquet(str(output))
                    cursor = db.execute(
                        "SELECT * FROM read_parquet(?) LIMIT ?",
                        [str(output), rows + 1],
                    )
                else:
                    cursor = db.sql(sql, params=parameters or []).limit(rows + 1)
                result = _preview(cursor, rows)
            finally:
                deadline.cancel()
                deadline.join()

        result["inputs"] = refs
        if save_as:
            result["saved"] = workbook.save_file(
                output,
                name=save_as,
                kind="table",
                media_type=PARQUET,
                summary="Result of a saved DuckDB query.",
                source="DuckDB SELECT",
                dependencies=refs,
                expected_version=expected_version,
                recipe={"sql": sql, "parameters": parameters or []},
            )
        return result


def refresh(workbook: Workbook, ref: str) -> dict:
    saved = workbook.inspect(ref, include_recipe=True)
    if "recipe" not in saved or not saved["is_latest"]:
        raise ValueError("Refresh requires the current version of a saved query")
    latest_names = [workbook.inspect(item)["name"] for item in saved["dependencies"]]
    return query(
        workbook,
        latest_names,
        **saved["recipe"],
        save_as=saved["name"],
        expected_version=saved["version"],
    )
