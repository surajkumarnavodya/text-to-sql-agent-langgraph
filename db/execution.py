"""Read-only SQL execution mechanics: timeout enforcement + row-cap fetch.

Shared by `agent.nodes.execute_sql_node` (the agent's internal self-correction
loop) and `ui/app.py`'s manual "Confirm and Run" path, so both go through the
exact same connection reuse, timeout, and read-only enforcement -- neither the
agent nor the UI gets its own, separately-maintained execution logic. Lives in
`db/` (not `agent/`) because it is purely a database-execution concern with no
LangGraph/state dependency -- every other piece of DB-facing logic
(`db/connection.py`'s engine lifecycle, `db/query_cost.py`'s plan estimation)
already lives here too.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from typing import Any

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from config.settings import get_settings
from db.connection import get_read_only_engine

logger = logging.getLogger(__name__)

# DB_TYPE -> a one-line SET statement that caps execution time at the
# database itself, for engines where that's a cheap, well-known statement.
# Applied best-effort right after opening the connection, before running the
# actual query -- this is the "enforced at the driver level" half of the
# timeout story (see `_execute_with_timeout` for the other half, which
# covers every dialect including the two without a simple SET form below).
_STATEMENT_TIMEOUT_SQL: dict[str, str] = {
    "postgresql": "SET statement_timeout = {timeout_ms}",
    "mysql": "SET SESSION MAX_EXECUTION_TIME = {timeout_ms}",
}


def _apply_statement_timeout(connection, dialect_name: str, timeout_seconds: int) -> None:
    """Best-effort driver-level statement timeout, where a simple SET exists.

    Not every engine has a one-line session-level timeout (MSSQL/Oracle
    don't), which is why this is "best effort" and paired with the
    thread-based cancellation fallback in `_execute_with_timeout` that
    covers every dialect uniformly.
    """
    template = _STATEMENT_TIMEOUT_SQL.get(dialect_name)
    if not template:
        return
    timeout_ms = int(timeout_seconds * 1000)
    try:
        connection.execute(text(template.format(timeout_ms=timeout_ms)))
    except SQLAlchemyError as exc:
        # Non-fatal: the thread-based fallback below still enforces the
        # wall-clock cutoff even if this driver-level SET isn't permitted
        # for this user/role.
        logger.debug("[execute_sql] could not set driver-level statement timeout: %s", exc)


def _execute_with_timeout(
    sql: str, query_timeout_seconds: int, max_result_rows: int, engine: Engine | None = None
) -> tuple[list[str], list[tuple]]:
    """Runs `sql` on a worker thread and force-aborts it past `query_timeout_seconds`.

    SQLAlchemy has no universal, cross-dialect "cancel this query" call, so
    the fallback that works for every one of the four supported engines is:
    run the query on a background thread, and if it hasn't finished by the
    deadline, close the underlying connection from the *calling* thread.
    Closing the socket out from under an in-flight query forces the
    database server to notice and kill it -- a real, driver-level
    cancellation, not just "stop waiting for the response" on our side.

    Row cap is enforced with `fetchmany(max_result_rows)` rather than
    `fetchall()` -- a defense-in-depth measure independent of the `LIMIT`
    clause already added by `agent.sql_validator.enforce_row_limit`, so a
    malformed or dialect-mistranslated query that ignores/lacks a LIMIT
    still can't pull an unbounded result set into memory.

    Args:
        engine: The specific database to execute against. None falls back
            to the legacy global default connection (`get_read_only_engine()`
            with no argument) -- callers that resolved a specific database
            (e.g. `agent.nodes.execute_sql_node`, once a question has been
            auto-routed) must pass it explicitly instead.
    """
    engine = engine or get_read_only_engine()
    result: dict[str, Any] = {}
    error: dict[str, BaseException] = {}
    connection_holder: dict[str, Any] = {}

    def _run() -> None:
        try:
            with engine.connect() as connection:
                connection_holder["connection"] = connection
                _apply_statement_timeout(connection, engine.dialect.name, query_timeout_seconds)
                cursor_result = connection.execute(text(sql))
                result["columns"] = list(cursor_result.keys())
                result["rows"] = cursor_result.fetchmany(max_result_rows)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread below
            error["error"] = exc
        finally:
            connection_holder.pop("connection", None)

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(query_timeout_seconds)

    if worker.is_alive():
        connection = connection_holder.get("connection")
        if connection is not None:
            # Best-effort abort; must not mask the TimeoutError raised below.
            with contextlib.suppress(Exception):
                connection.close()
        worker.join(query_timeout_seconds)
        raise TimeoutError(f"Query exceeded the {query_timeout_seconds}s timeout and was aborted.")

    if "error" in error:
        raise error["error"]

    return result["columns"], result["rows"]


def execute_readonly_sql(
    sql: str,
    query_timeout_seconds: int,
    max_result_rows: int | None = None,
    engine: Engine | None = None,
) -> tuple[list[str], list[tuple]]:
    """Executes already-validated, already row-limited SQL read-only.

    Shared by `agent.nodes.execute_sql_node` (the agent's internal
    self-correction loop) and `ui/app.py`'s manual "Confirm and Run" path, so
    both go through the exact same connection reuse, timeout, and read-only
    enforcement -- the UI does not get its own, separately-maintained
    execution logic.

    Args:
        sql: Already-validated, row-limited SQL text.
        query_timeout_seconds: Wall-clock execution timeout.
        max_result_rows: Row cap; defaults to `Settings.max_result_rows`.
        engine: The specific database to execute against -- see
            `_execute_with_timeout`'s docstring. None (the default) keeps
            this function's original single-database behavior.

    Returns:
        (columns, rows).

    Raises:
        SQLAlchemyError: on a SQL execution error (e.g. unknown column).
        TimeoutError: if execution exceeds `query_timeout_seconds`.
    """
    resolved_max_rows = (
        max_result_rows if max_result_rows is not None else get_settings().max_result_rows
    )
    return _execute_with_timeout(sql, query_timeout_seconds, resolved_max_rows, engine=engine)
