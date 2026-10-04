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

**Prompt 06** (`06_DATABASE_DISCOVERY_CONTRACT.md`) extended this module:
views are now discovered alongside tables (`TableSchemaInfo.is_view`);
`ColumnInfo` gained `default`/`is_computed`/`is_identity`/`length`/
`precision`/`scale`, all sourced from data the `Inspector` already
returns per column -- no new query. One table/view's own introspection
failing (a permissions issue on a specific object, a dialect quirk) no
longer aborts the whole database -- see `introspect_schema`'s own
docstring for the per-object isolation this now has.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, replace

from sqlalchemy import Engine, inspect
from sqlalchemy.engine.interfaces import ReflectedColumn
from sqlalchemy.engine.reflection import Inspector

from db.row_count_estimate import estimate_row_count
from security.sanitization import normalize_text

logger = logging.getLogger(__name__)

# Generous cap for any real database identifier (every supported engine's
# own identifier-length limit is well under this) -- defense-in-depth only,
# not the primary reason this is safe: see render_ddl's docstring.
_MAX_IDENTIFIER_LENGTH = 128


@dataclass(frozen=True)
class ColumnInfo:
    """One column's shape, as introspected.

    `default`/`is_computed`/`is_identity`/`length`/`precision`/`scale`
    (Prompt 06) are all sourced from data SQLAlchemy's `Inspector` already
    returns per column -- no second query. Each defaults to `None`/`False`
    when a dialect doesn't populate it (fail-open, never a reason
    introspection itself fails) rather than raising.
    """

    name: str
    type: str
    nullable: bool
    is_primary_key: bool
    default: str | None = None
    is_computed: bool = False
    is_identity: bool = False
    length: int | None = None
    precision: int | None = None
    scale: int | None = None


@dataclass(frozen=True)
class ForeignKeyInfo:
    """One foreign key relationship, as introspected."""

    constrained_columns: tuple[str, ...]
    referred_table: str
    referred_columns: tuple[str, ...]


@dataclass(frozen=True)
class TableSchemaInfo:
    """One table's (or view's) full introspected shape, plus a synthesized DDL-like rendering.

    `ddl` is not necessarily valid, executable DDL for every engine -- it's
    a compact, LLM-friendly `CREATE TABLE`-style text rendering of the
    introspected columns/keys, chosen because that's the format the DDL-fed
    prompt used before (and what most SQL-generation models are tuned on),
    now synthesized from live metadata instead of a hand-written file.

    `is_view` (Prompt 06): whether this object is a view rather than a base
    table -- `render_ddl` renders `CREATE VIEW` instead of `CREATE TABLE`
    for one, so the LLM isn't told a view is something it can be joined
    against like an indexed table without qualification. Views are
    discovered and embedded exactly like tables otherwise (same retrieval,
    same validation, same execution path) -- there is no separate
    "view mode" anywhere downstream.

    `row_count_estimate` (Prompt 06): an approximate, catalog-only row
    count (see `db/row_count_estimate.py`) -- `None` unless
    `introspect_schema` was called with `include_row_counts=True` (opt-in,
    default off, so every existing caller's per-refresh cost is unchanged
    unless it asks for this). Never embedded into `ddl`/the LLM prompt --
    discovery/onboarding metadata only.

    `unique_constraints` (Prompt 07,
    `07_RELATIONSHIP_INTELLIGENCE_CONTRACT.md`): each entry is one unique
    constraint's column names, from `Inspector.get_unique_constraints` --
    catalog-only, same cost class as the existing PK/FK calls. This is what
    `db/relationship_inference.py` checks to decide whether a column is
    even a *legal* foreign-key target (a real FK's target is always a
    primary key or a unique constraint) -- a bare index is not the same
    guarantee (a non-unique index allows duplicates), so this is
    deliberately not folded together with a future "indexes" field.
    """

    table_name: str
    columns: tuple[ColumnInfo, ...]
    foreign_keys: tuple[ForeignKeyInfo, ...]
    ddl: str
    is_view: bool = False
    row_count_estimate: int | None = None
    unique_constraints: tuple[tuple[str, ...], ...] = ()


def _sanitize_identifier(value: str) -> str:
    """Normalizes+caps one identifier (table/column/type name) for DDL rendering.

    Every supported database engine constrains what a real identifier can
    contain at CREATE TABLE time (no engine permits an embedded newline or
    control character), so this is defense-in-depth rather than the primary
    guarantee for identifiers specifically -- unlike sampled *values* (see
    `db/value_sampling.py`), which are genuinely attacker-writable data with
    no such structural constraint. Applied uniformly anyway, since "the
    engine probably wouldn't allow it" is a weaker claim than "this function
    makes it safe regardless," and the cost of applying it is negligible.

    Deliberately a *local*, render-time transformation -- `ColumnInfo` /
    `TableSchemaInfo` / `ForeignKeyInfo` themselves keep the original,
    unsanitized names, because those are also used for real operations
    (`db.value_sampling`'s `SELECT DISTINCT {column} FROM {table}`,
    `Inspector` calls keyed by table name) where substituting a
    normalized-but-different string would break correctness, not just
    cosmetics. Only the rendered *text* going into the prompt needs this.
    """
    return normalize_text(value)[:_MAX_IDENTIFIER_LENGTH]


def render_ddl(
    table_name: str,
    columns: tuple[ColumnInfo, ...],
    foreign_keys: tuple[ForeignKeyInfo, ...],
    sample_values: dict[str, tuple[str, ...]] | None = None,
    is_view: bool = False,
) -> str:
    """Synthesizes a compact CREATE-TABLE-style text block for one table (or view).

    Every identifier (table name, column names/types, FK table/column
    names) is normalized via `_sanitize_identifier` before being formatted
    into the returned text -- this function's output is exactly what ends
    up embedded in Chroma and concatenated into the LLM prompt (see
    `agent.nodes.retrieve_schema_node`), so this is the boundary where
    database-sourced text must already be safe. `column.default`'s value
    (an arbitrary DB-sourced expression string, e.g. `"((0))"`) is
    sanitized the same way before rendering, for the same reason.

    Args:
        sample_values: Optional column name -> distinct values actually seen
            in the data (e.g. {"ProductLine": ("M", "R", "S", "T")}), rendered
            as a trailing comment on that column's line. This is the only
            data-derived input to an otherwise metadata-only renderer -- see
            `db/value_sampling.py`, which is the sole caller that ever passes
            a non-None value, and which is responsible for sanitizing the
            *values* themselves at the point they're fetched (this function
            trusts they're already clean by the time they arrive here, but
            still looks them up by each column's original, unsanitized
            name, since that's the key `db.value_sampling.attach_sample_values`
            populates). Plain `introspect_schema()` always passes None,
            keeping its own "never touches table data" contract intact.
        is_view: Renders `CREATE VIEW` instead of `CREATE TABLE` (Prompt 06)
            -- purely a labeling difference for the LLM's benefit; the rest
            of the rendering (columns, FKs, sample values) is identical.
    """
    safe_table_name = _sanitize_identifier(table_name)
    keyword = "VIEW" if is_view else "TABLE"
    lines = [f"CREATE {keyword} {safe_table_name} ("]
    column_lines = []
    for column in columns:
        safe_name = _sanitize_identifier(column.name)
        safe_type = _sanitize_identifier(column.type)
        parts = [f"    {safe_name} {safe_type}"]
        if column.is_primary_key:
            parts.append("PRIMARY KEY")
        if not column.nullable and not column.is_primary_key:
            parts.append("NOT NULL")
        if column.is_identity:
            parts.append("IDENTITY")
        if column.is_computed:
            parts.append("COMPUTED")
        if column.default is not None:
            parts.append(f"DEFAULT {_sanitize_identifier(column.default)}")
        # Looked up by the column's *original* name -- that's the key
        # db.value_sampling.attach_sample_values populates -- but the
        # values themselves are already sanitized at their source
        # (db.value_sampling._sample_column), not re-sanitized here.
        # Always last: a SQL line comment consumes the rest of the line.
        values = (sample_values or {}).get(column.name)
        if values:
            parts.append(f"-- e.g. {', '.join(values)}")
        column_lines.append(" ".join(parts))
    for fk in foreign_keys:
        constrained = ", ".join(_sanitize_identifier(c) for c in fk.constrained_columns)
        referred_table = _sanitize_identifier(fk.referred_table)
        referred = ", ".join(_sanitize_identifier(c) for c in fk.referred_columns)
        column_lines.append(
            f"    FOREIGN KEY ({constrained}) REFERENCES {referred_table} ({referred})"
        )
    lines.append(",\n".join(column_lines))
    lines.append(");")
    return "\n".join(lines)


# One-column-per-line, 4-space-indented -- matches `render_ddl`'s own
# rendering exactly. `FOREIGN KEY (...)`/`PRIMARY KEY (...)` table-level
# constraint lines are skipped explicitly below rather than relying on
# indentation alone, since both would otherwise match this same pattern as
# a plausible-looking "column name" ("FOREIGN"/"PRIMARY").
_DDL_COLUMN_LINE_RE = re.compile(r"^\s{4}(\w+)\s", re.MULTILINE)

# `    FOREIGN KEY (col1, col2) REFERENCES TableName (col1, col2)` -- the
# exact shape `render_ddl` emits above. Only the referred table name is
# captured; the referenced/constrained column lists aren't needed by any
# current caller (relationship-*path* existence, not column-level join
# correctness, which `agent.sql_validator` already checks on the real
# generated SQL).
_DDL_FOREIGN_KEY_RE = re.compile(
    r"FOREIGN KEY\s*\([^)]*\)\s*REFERENCES\s+(\S+)\s*\(", re.IGNORECASE
)


def extract_ddl_column_names(ddl: str) -> list[str]:
    """Pulls column names out of one table's `render_ddl`-rendered DDL text.

    Shared by `agent.nodes._suggest_correct_column` (a "did you mean"
    suggestion when generated SQL references a missing column) and
    `agent.plan_validator.validate_plan` (Prompt 12,
    `12_ANALYTICAL_PLANNING_CONTRACT.md` -- checking that a structured
    plan's metric/dimension/filter column references actually exist) --
    one implementation, not two, per master-contract rule 2/3. Relies only
    on `render_ddl`'s consistent one-column-per-line rendering; skips the
    `CREATE TABLE`/closing-paren bookend lines and `FOREIGN KEY (...)`/
    `PRIMARY KEY (...)` table-level constraint lines.
    """
    names = []
    for line in ddl.splitlines():
        stripped = line.strip().rstrip(",")
        if not stripped or stripped.startswith(
            ("CREATE TABLE", "CREATE VIEW", "FOREIGN KEY", "PRIMARY KEY", ")")
        ):
            continue
        first_token = stripped.split(None, 1)[0]
        if first_token.isidentifier():
            names.append(first_token)
    return names


def extract_ddl_foreign_key_targets(ddl: str) -> list[str]:
    """Pulls the *referred table* name out of every `FOREIGN KEY (...)
    REFERENCES <table> (...)` line in one table's `render_ddl`-rendered DDL
    text -- used by `agent.plan_validator.validate_plan` (Prompt 12) to
    build a bounded join-path graph over the tables already retrieved for
    this question.

    Deliberately scoped to table names only (not the constrained/referred
    column lists) -- relationship-*path* existence, not column-level join
    correctness, which `agent.sql_validator` already checks on the real
    generated SQL. A disclosed, bounded scope: this only sees FK edges
    declared in DDL text already shown to the LLM for this question, never
    a full cross-schema graph search or an inferred (non-FK) candidate
    relationship (see `db/relationship_inference.py`) -- those are a
    different, lower-confidence kind of claim this deterministic check
    deliberately does not lean on.
    """
    return [match.group(1) for match in _DDL_FOREIGN_KEY_RE.finditer(ddl)]


def _build_column_info(col: ReflectedColumn) -> ColumnInfo:
    """Builds one `ColumnInfo` from one raw column dict returned by
    `Inspector.get_columns()`.

    `length`/`precision`/`scale` come from the reflected `type` object
    itself (e.g. `String(50).length == 50`, `Numeric(18, 2).precision ==
    18`), not a second query -- `getattr(..., None)` is a genuine `None`
    for a type that doesn't have that attribute at all (e.g. `INTEGER` has
    no `.length`), not a missing-value sentinel. `default`/`computed`/
    `identity` are dialect-populated keys on the same dict that not every
    engine/dialect reflects -- `.get(...)`, defaulting to `None`, keeps
    this fail-open rather than raising on an engine that doesn't.
    """
    col_type = col["type"]
    computed = col.get("computed")
    identity = col.get("identity")
    default = col.get("default")
    return ColumnInfo(
        name=str(col["name"]),
        type=str(col_type),
        nullable=bool(col.get("nullable", True)),
        is_primary_key=False,  # set by the caller, which knows the PK constraint
        default=str(default) if default is not None else None,
        is_computed=computed is not None,
        is_identity=identity is not None,
        length=getattr(col_type, "length", None),
        precision=getattr(col_type, "precision", None),
        scale=getattr(col_type, "scale", None),
    )


def _unique_column_sets(
    inspector: Inspector, name: str, schema: str | None
) -> tuple[tuple[str, ...], ...]:
    """Column sets that are guaranteed unique in one table.

    Prefers the dialect's own UNIQUE-constraint reflection. Some dialects do not
    implement it -- SQLAlchemy 2.0's mssql dialect raises NotImplementedError --
    so fall back to UNIQUE indexes, which enforce the same guarantee for the
    relationship-inference check. An unsupported or failing reflection yields no
    unique sets rather than skipping the whole table.
    """
    try:
        return tuple(
            tuple(uc["column_names"])
            for uc in inspector.get_unique_constraints(name, schema=schema)
            if uc.get("column_names")
        )
    except NotImplementedError:
        pass
    try:
        return tuple(
            tuple(ix["column_names"])
            for ix in inspector.get_indexes(name, schema=schema)
            if ix.get("unique")
            and ix.get("column_names")
            and all(col is not None for col in ix["column_names"])
        )
    except NotImplementedError:
        return ()


def _introspect_one(
    inspector: Inspector,
    name: str,
    schema: str | None,
    is_view: bool,
    engine: Engine | None = None,
    db_type: str | None = None,
) -> TableSchemaInfo:
    """Introspects one table or view -- the unit `introspect_schema` isolates
    a per-object failure to (see that function's own docstring).

    `engine`/`db_type` are only used for the opt-in row-count estimate
    (`include_row_counts=True`) -- both `None` (the default) skips it
    entirely, no extra query issued.
    """
    pk_columns: set[str] = set()
    if not is_view:
        # Views have no PK constraint to speak of; get_pk_constraint on a
        # view raises for some dialects, so it's skipped entirely rather
        # than called-and-caught.
        pk_constraint = inspector.get_pk_constraint(name, schema=schema)
        pk_columns = set(pk_constraint.get("constrained_columns") or [])

    columns = tuple(
        replace(_build_column_info(col), is_primary_key=col["name"] in pk_columns)
        for col in inspector.get_columns(name, schema=schema)
    )

    foreign_keys: tuple[ForeignKeyInfo, ...] = ()
    unique_constraints: tuple[tuple[str, ...], ...] = ()
    if not is_view:
        # Views don't carry their own FK/unique constraints either.
        foreign_keys = tuple(
            ForeignKeyInfo(
                constrained_columns=tuple(fk["constrained_columns"]),
                referred_table=fk["referred_table"],
                referred_columns=tuple(fk["referred_columns"]),
            )
            for fk in inspector.get_foreign_keys(name, schema=schema)
            if fk.get("constrained_columns") and fk.get("referred_table")
        )
        unique_constraints = _unique_column_sets(inspector, name, schema)

    ddl = render_ddl(name, columns, foreign_keys, is_view=is_view)

    row_count_estimate: int | None = None
    if engine is not None and db_type is not None:
        row_count_estimate = estimate_row_count(engine, name, schema, db_type)

    return TableSchemaInfo(
        table_name=name,
        columns=columns,
        foreign_keys=foreign_keys,
        ddl=ddl,
        is_view=is_view,
        row_count_estimate=row_count_estimate,
        unique_constraints=unique_constraints,
    )


def introspect_schema(
    engine: Engine, schema: str | None = None, include_row_counts: bool = False
) -> list[TableSchemaInfo]:
    """Introspects every table and view (name, columns, types, FKs) visible
    to the connection.

    Args:
        engine: A SQLAlchemy engine -- typically `db.connection.get_read_only_engine()`.
            Introspection only ever issues metadata queries (reads
            `information_schema`/catalog views), never touches table data.
        schema: Optional schema name to restrict introspection to (from
            `Settings.db_schema`). None means "the database's default
            schema for this connection" (SQLAlchemy's own default behavior).
        include_row_counts: Opt-in (Prompt 06, default `False`) -- when
            `True`, also issues one extra catalog-only query per table/view
            (`db.row_count_estimate.estimate_row_count`) and populates
            `TableSchemaInfo.row_count_estimate`. Off by default so every
            existing caller's per-refresh cost (one query per table plus
            two for PK/FK, already the case before this prompt) is
            unchanged unless it explicitly asks for the extra round trip.

    Returns:
        One `TableSchemaInfo` per table/view, ordered by name for
        deterministic output (which matters for the schema-fingerprint hash
        used by `embeddings/schema_indexer.py`'s cache invalidation).

    A single table/view whose own introspection fails (a permissions
    issue on that one object, a dialect quirk `Inspector` chokes on) is
    logged and skipped -- Prompt 06
    (`06_DATABASE_DISCOVERY_CONTRACT.md`) -- rather than aborting
    discovery for every other, perfectly-introspectable object in the same
    database. This mirrors `embeddings.schema_indexer
    .refresh_all_schema_indexes`'s existing "one bad database must not
    block the others" posture, one level down (one bad object must not
    block the others in the same database).
    """
    inspector = inspect(engine)
    table_names = sorted(inspector.get_table_names(schema=schema))
    try:
        view_names = sorted(inspector.get_view_names(schema=schema))
    except NotImplementedError:
        # A handful of third-party dialects don't implement view reflection
        # at all -- treated exactly like "this database has no views",
        # never a reason table discovery itself fails.
        view_names = []
    logger.info(
        "Introspecting %d table(s) and %d view(s) in schema=%r",
        len(table_names),
        len(view_names),
        schema,
    )

    row_count_engine = engine if include_row_counts else None
    row_count_db_type = engine.dialect.name if include_row_counts else None

    tables: list[TableSchemaInfo] = []
    for name, is_view in [(t, False) for t in table_names] + [(v, True) for v in view_names]:
        try:
            tables.append(
                _introspect_one(
                    inspector,
                    name,
                    schema,
                    is_view,
                    engine=row_count_engine,
                    db_type=row_count_db_type,
                )
            )
        except Exception as exc:  # noqa: BLE001 - one bad object must not block the rest
            logger.warning(
                "Skipping %s %r in schema=%r: introspection failed: %s",
                "view" if is_view else "table",
                name,
                schema,
                exc,
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
