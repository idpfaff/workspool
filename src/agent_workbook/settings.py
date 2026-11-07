"""Tunable limits shared by the store, the query engine, the CLI, and the server."""

import re
from dataclasses import dataclass

MEMORY_LIMIT = re.compile(r"\d+(\.\d+)?\s*[KMGT]i?B", flags=re.IGNORECASE)


@dataclass(frozen=True)
class Settings:
    """Bounds that keep the workbook predictable on a local machine.

    Every field has a conservative default. Raise them from the CLI when the
    machine and the task call for it; they are validated once, up front.
    """

    index_bytes: int = 65536
    """Bytes of each text artifact indexed for `find` (counted from the start)."""

    query_timeout: float = 15.0
    """Seconds before a running SELECT is interrupted."""

    query_memory: str = "1GB"
    """DuckDB memory limit, such as 512MB or 4GB."""

    query_threads: int = 2
    """DuckDB threads per query."""

    max_preview_rows: int = 25
    """Upper bound on the rows a query preview returns in one reply."""

    def __post_init__(self) -> None:
        if not 1024 <= self.index_bytes <= 16 * 1024 * 1024:
            raise ValueError("index_bytes must be between 1 KiB and 16 MiB")
        if not 0 < self.query_timeout <= 3600:
            raise ValueError("query_timeout must be between 0 and 3600 seconds")
        if not MEMORY_LIMIT.fullmatch(self.query_memory.strip()):
            raise ValueError("query_memory must look like 512MB, 1GB, or 2GiB")
        if not 1 <= self.query_threads <= 256:
            raise ValueError("query_threads must be between 1 and 256")
        if not 1 <= self.max_preview_rows <= 1000:
            raise ValueError("max_preview_rows must be between 1 and 1000")
