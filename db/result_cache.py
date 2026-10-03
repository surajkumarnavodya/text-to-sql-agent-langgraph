"""Opt-in, bounded, in-process TTL+LRU cache for `POST /execute`'s result sets.

Prompt 22 (scale/performance hardening) -- closes the one gap
`docs/SCALE_OUT_PROMPT.md`'s Phase 4 explicitly named as missing: "Result
cache for identical SQL on the same database with a short TTL, opt-in per
database... never for queries touching sensitive columns." (This pass
implements it as a single global opt-in rather than truly per-database --
see this module's own "Known limitations" note below.)

Scope, deliberately narrow:
  - Only `api/main.py`'s `POST /execute` ("Confirm and Run") wires this in.
    `agent.nodes.execute_sql_node`'s own internal self-correction retry
    executions are never cached -- those results aren't shown to the user,
    and a retry attempt changing the SQL text (the whole point of a retry)
    means an identical-SQL cache hit there would be rare and not worth the
    added risk of masking a real self-correction signal.
  - Default **off** (`Settings.enable_result_cache`) -- caching is a
    deliberate, data-freshness-affecting operator choice, not a safe-by-
    default accuracy aid. This mirrors this codebase's existing convention
    for anything with trust/freshness implications (`ENABLE_DOCUMENT_RAG`,
    `ENABLE_WEB_SEARCH`, ...), not a new pattern.
  - Never caches any SQL referencing a `config.sensitive_columns`-classified
    "restricted" column, regardless of the executing caller's own
    permission (`is_cacheable_sql`) -- the simplest safe rule: a cached row
    set must never be served to a *different* caller than the one who
    produced it, and this cache has no per-caller/per-permission
    partitioning at all, so anything sensitivity-classified is excluded
    from caching entirely rather than attempting to reason about who may
    see a cached hit.
  - **Partitioned by tenant** (Prompt 20): the cache key is
    `(tenant_id, database_name, sql)`, never `(database_name, sql)` alone.
    Two tenants permitted to query the same configured database can
    legitimately run byte-identical SQL, and serving one tenant's rows to
    the other would be a cross-tenant data leak through a side channel no
    ABAC check covers -- the cache sits *after* every authorization gate,
    so it cannot rely on them. This is why `tenant_id` is a **required**
    parameter on both `get` and `store` rather than an optional one: an
    omitted tenant would silently re-create the shared-key bug.

Known limitations (disclosed, not hidden):
  - Exact-text match only, not semantic -- two questions whose generated
    SQL differs by even whitespace or alias naming are two different cache
    keys. A real semantic cache (embedding similarity over prior approved
    answers) is `docs/SCALE_OUT_PROMPT.md` Phase 4's own, larger, deferred
    scope -- this is the narrower, safe slice of that idea.
  - A single global on/off switch, not truly per-database as that Phase 4
    bullet describes -- every configured database shares the same
    enable flag/TTL/size bound today (though never the same cache *entries*,
    since the cache key always includes the database name). Per-database
    granularity is a reasonable, deferred follow-up once there's a real
    need for one database's freshness requirements to differ from
    another's.
  - Single-process, in-memory, resets on restart -- the same disclosed
    limitation every other process-local cache/limiter in this codebase
    already carries (see `agent/rate_limit.py`'s own module docstring).
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import OrderedDict

from agent.sql_validator import find_restricted_column_references
from config.sensitive_columns import load_sensitive_columns

logger = logging.getLogger(__name__)


def is_cacheable_sql(sql: str, dialect: str | None) -> bool:
    """Whether `sql` is safe to cache -- `False` if it references any
    `config.sensitive_columns`-classified "restricted" column.

    `known_tables` is deliberately not accepted/threaded through here: the
    only real caller (`api/main.py`'s `POST /execute`) has no retrieved-
    schema context to supply one from (unlike `agent.nodes.validate_sql_node`,
    which already has `state["schema_tables"]`), and
    `find_restricted_column_references`'s own docstring confirms that
    parameter is unused by its matching logic -- grounded in the
    statement's own parsed `FROM`/`JOIN` tables instead. Passing an empty
    set here is exactly as accurate as passing any other set would be.
    """
    classifications = load_sensitive_columns()
    restricted_pairs = {pair for pair, tier in classifications.items() if tier == "restricted"}
    if not restricted_pairs:
        return True
    hits = find_restricted_column_references(sql, restricted_pairs, set(), dialect=dialect)
    return not hits


def _cache_key(tenant_id: str, database_name: str, sql: str) -> str:
    """SHA-256 of `tenant_id + database_name + sql` -- never logged, just an
    opaque dict key.

    All three components are load-bearing: the database name keeps two
    databases that happen to generate identical SQL text from colliding in
    the same cache slot, and `tenant_id` (Prompt 20) keeps one tenant's
    rows from ever being served to another that ran the same query against
    the same shared database. The newline separators make the concatenation
    unambiguous, so two different component splits can never collide."""
    return hashlib.sha256(f"{tenant_id}\n{database_name}\n{sql}".encode()).hexdigest()


class ResultCache:
    """Bounded (`max_entries`, LRU-evicted) in-process cache of
    `(columns, rows)` by `(database_name, sql)`, with a per-get TTL check.

    Same `OrderedDict` + `threading.Lock` LRU shape as
    `agent.rate_limit.BoundedLimiterCache` -- deliberately reused rather
    than a new caching primitive. TTL is checked at read time (an expired
    entry is evicted on the `get()` that discovers it, not swept by a
    background thread) -- simplest correct approach at this cache's actual
    scale (at most `max_entries` entries, each a cheap dict lookup away).
    """

    def __init__(self, max_entries: int = 500) -> None:
        self._max_entries = max_entries
        self._lock = threading.Lock()
        # value: (columns, rows, stored_at_monotonic)
        self._entries: OrderedDict[str, tuple[list[str], list[tuple], float]] = OrderedDict()

    def get(
        self,
        tenant_id: str,
        database_name: str,
        sql: str,
        ttl_seconds: float,
        now: float | None = None,
    ) -> tuple[list[str], list[tuple]] | None:
        """Returns the cached `(columns, rows)` for this exact
        `(tenant_id, database_name, sql)`, or `None` on a miss or an expired
        entry."""
        current = time.monotonic() if now is None else now
        key = _cache_key(tenant_id, database_name, sql)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            columns, rows, stored_at = entry
            if current - stored_at > ttl_seconds:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return columns, rows

    def store(
        self,
        tenant_id: str,
        database_name: str,
        sql: str,
        columns: list[str],
        rows: list[tuple],
        now: float | None = None,
    ) -> None:
        """Records a fresh result, evicting the least-recently-used entry
        past `max_entries`."""
        current = time.monotonic() if now is None else now
        key = _cache_key(tenant_id, database_name, sql)
        with self._lock:
            self._entries[key] = (columns, rows, current)
            self._entries.move_to_end(key)
            if len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
                logger.debug(
                    "[result_cache] at capacity (%d entries) -- evicted LRU key to admit a new one",
                    self._max_entries,
                )

    def clear(self) -> None:
        """Drops every entry. Mainly for tests."""
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


_result_cache: ResultCache | None = None


def get_result_cache(max_entries: int) -> ResultCache:
    """Returns the process-wide result cache, creating it on first use --
    same first-call-wins singleton pattern as
    `agent.rate_limit.get_ask_concurrency_limiter` (a config change to
    `max_entries` needs a process restart to take effect, consistent with
    every other process-lifetime singleton in this codebase)."""
    global _result_cache
    if _result_cache is None:
        _result_cache = ResultCache(max_entries=max_entries)
    return _result_cache
