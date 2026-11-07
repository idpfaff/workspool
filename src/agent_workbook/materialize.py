"""Prevent DuckDB's implicit HUGEINT-to-DOUBLE Parquet conversion."""


def lossless_parquet_projection(relation):
    """Return (lazy relation, explicit type conversions), without executing it.

    Parquet has no signed/unsigned 128-bit integer primitive. Decimal(38,0)
    preserves values within its range; DuckDB rejects out-of-range casts while
    materializing, instead of silently rounding through DOUBLE. Nested HUGEINT
    types require an explicit user projection, because rewriting nested schemas
    can alter field identity or null semantics.
    """
    names, types = relation.columns, [str(kind) for kind in relation.types]
    if any("HUGEINT" in kind and kind not in {"HUGEINT", "UHUGEINT"} for kind in types):
        raise ValueError(
            "Project nested HUGEINT values explicitly to DECIMAL(38,0) before Parquet storage"
        )
    conversions = [
        {"column": name, "source_type": kind, "stored_type": "DECIMAL(38,0)"}
        for name, kind in zip(names, types, strict=True)
        if kind in {"HUGEINT", "UHUGEINT"}
    ]
    if not conversions:
        return relation, []
    if len(set(names)) != len(names):
        raise ValueError("Use unique output aliases when materializing HUGEINT columns")
    expressions = []
    for name, kind in zip(names, types, strict=True):
        quoted = '"' + name.replace('"', '""') + '"'
        expressions.append(
            f"CAST({quoted} AS DECIMAL(38,0)) AS {quoted}"
            if kind in {"HUGEINT", "UHUGEINT"}
            else quoted
        )
    return relation.project(",".join(expressions)), conversions
