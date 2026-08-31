"""Live database schema introspection.

This is the sole source of truth for what the LLM is told about the
database's shape -- there is no bundled/hardcoded schema file anywhere in
the project anymore. `embeddings/schema_indexer.py` embeds exactly the
`TableSchemaInfo` objects this module produces, and nothing else.

Uses SQLAlchemy's `Inspector` (`sqlalchemy.inspect`), which works uniformly
across all four supported `DB_TYPE`s via `information_schema`/catalog
queries under the hood -- no per-database-engine introspection code needed
here.

`introspect_schema()` itself never touches table data, only catalog
metadata. `db/value_sampling.py` is the separate, explicitly-opt-in module
that *does* query real column values (for a bounded set of low-cardinality
columns) and re-renders DDL via this module's `render_ddl()` to include
them -- kept separate so this module's "metadata only" contract stays true
by construction, not by convention.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from sqlalchemy import Engine, inspect

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ColumnInfo:
    """One column's shape, as introspected."""

    name: str
    type: str
    nullable: bool
    is_primary_key: bool


@dataclass(frozen=True)
class ForeignKeyInfo:
    """One foreign key relationship, as introspected."""

    constrained_columns: tuple[str, ...]
    referred_table: str
    referred_columns: tuple[str, ...]


@dataclass(frozen=True)
class TableSchemaInfo:
    """One table's full introspected shape, plus a synthesized DDL-like rendering.

    `ddl` is not necessarily valid, executable DDL for every engine -- it's
    a compact, LLM-friendly `CREATE TABLE`-style text rendering of the
    introspected columns/keys, chosen because that's the format the DDL-fed
    prompt used before (and what most SQL-generation models are tuned on),
    now synthesized from live metadata instead of a hand-written file.
    """

    table_name: str
    columns: tuple[ColumnInfo, ...]
    foreign_keys: tuple[ForeignKeyInfo, ...]
    ddl: str


def render_ddl(
    table_name: str,
    columns: tuple[ColumnInfo, ...],
    foreign_keys: tuple[ForeignKeyInfo, ...],
    sample_values: dict[str, tuple[str, ...]] | None = None,
) -> str:
    """Synthesizes a compact CREATE-TABLE-style text block for one table.

    Args:
        sample_values: Optional column name -> distinct values actually seen
            in the data (e.g. {"ProductLine": ("M", "R", "S", "T")}), rendered
            as a trailing comment on that column's line. This is the only
            data-derived input to an otherwise metadata-only renderer -- see
            `db/value_sampling.py`, which is the sole caller that ever passes
            a non-None value. Plain `introspect_schema()` always passes None,
            keeping its own "never touches table data" contract intact.
    """
    lines = [f"CREATE TABLE {table_name} ("]
    column_lines = []
    for column in columns:
        parts = [f"    {column.name} {column.type}"]
        if column.is_primary_key:
            parts.append("PRIMARY KEY")
        if not column.nullable and not column.is_primary_key:
            parts.append("NOT NULL")
        values = (sample_values or {}).get(column.name)
        if values:
            parts.append(f"-- e.g. {', '.join(values)}")
        column_lines.append(" ".join(parts))
    for fk in foreign_keys:
        constrained = ", ".join(fk.constrained_columns)
        referred = ", ".join(fk.referred_columns)
        column_lines.append(
            f"    FOREIGN KEY ({constrained}) REFERENCES {fk.referred_table} ({referred})"
        )
    lines.append(",\n".join(column_lines))
    lines.append(");")
    return "\n".join(lines)


def introspect_schema(engine: Engine, schema: str | None = None) -> list[TableSchemaInfo]:
    """Introspects every table (name, columns, types, FKs) visible to the connection.

    Args:
        engine: A SQLAlchemy engine -- typically `db.connection.get_read_only_engine()`.
            Introspection only ever issues metadata queries (reads
            `information_schema`/catalog views), never touches table data.
        schema: Optional schema name to restrict introspection to (from
            `Settings.db_schema`). None means "the database's default
            schema for this connection" (SQLAlchemy's own default behavior).

    Returns:
        One `TableSchemaInfo` per table, ordered by table name for
        deterministic output (which matters for the schema-fingerprint hash
        used by `embeddings/schema_indexer.py`'s cache invalidation).
    """
    inspector = inspect(engine)
    table_names = sorted(inspector.get_table_names(schema=schema))
    logger.info("Introspected %d table(s) in schema=%r", len(table_names), schema)

    tables: list[TableSchemaInfo] = []
    for table_name in table_names:
        pk_constraint = inspector.get_pk_constraint(table_name, schema=schema)
        pk_columns = set(pk_constraint.get("constrained_columns") or [])

        columns = tuple(
            ColumnInfo(
                name=col["name"],
                type=str(col["type"]),
                nullable=bool(col.get("nullable", True)),
                is_primary_key=col["name"] in pk_columns,
            )
            for col in inspector.get_columns(table_name, schema=schema)
        )

        foreign_keys = tuple(
            ForeignKeyInfo(
                constrained_columns=tuple(fk["constrained_columns"]),
                referred_table=fk["referred_table"],
                referred_columns=tuple(fk["referred_columns"]),
            )
            for fk in inspector.get_foreign_keys(table_name, schema=schema)
            if fk.get("constrained_columns") and fk.get("referred_table")
        )

        ddl = render_ddl(table_name, columns, foreign_keys)
        tables.append(
            TableSchemaInfo(
                table_name=table_name,
                columns=columns,
                foreign_keys=foreign_keys,
                ddl=ddl,
            )
        )

    return tables


def get_schema_fingerprint(tables: list[TableSchemaInfo]) -> str:
    """Deterministic hash of an introspected schema, for embedding cache invalidation.

    Replaces the old file-hash approach (which hashed `db/schema.sql`'s
    bytes): there's no file anymore, so this hashes a deterministic
    serialization of the introspected tables instead. Same idea -- skip
    re-embedding in `embeddings/schema_indexer.py` when this hasn't changed
    since the last build.
    """
    serialized = "\n".join(table.ddl for table in sorted(tables, key=lambda t: t.table_name))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
