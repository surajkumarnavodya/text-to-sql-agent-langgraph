"""SQLAlchemy-based connection to the user's real database, plus schema introspection."""

from db.connection import (
    ConnectionTestResult,
    get_engine,
    get_read_only_engine,
    test_connection,
)
from db.execution import execute_readonly_sql
from db.schema_introspection import TableSchemaInfo, introspect_schema

__all__ = [
    "ConnectionTestResult",
    "TableSchemaInfo",
    "execute_readonly_sql",
    "get_engine",
    "get_read_only_engine",
    "introspect_schema",
    "test_connection",
]
