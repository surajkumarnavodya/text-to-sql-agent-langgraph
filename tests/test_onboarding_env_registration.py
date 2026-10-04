"""Tests for onboarding.env_registration: publishing writes a DB_CONNECTIONS entry to .env.

Every test uses a temporary .env file -- the real project .env is never touched.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from dotenv import dotenv_values

from config.settings import DatabaseConnectionConfig, _parse_named_connections
from onboarding.env_registration import (
    PublishedDatabase,
    apply_to_runtime,
    register_published_database,
    rollback_registration,
)


@pytest.fixture(autouse=True)
def _restore_process_environment() -> Iterator[None]:
    """apply_to_runtime writes os.environ; restore it so no test leaks DB_* variables."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


def _db(**overrides) -> PublishedDatabase:
    base = dict(
        label="Employee management database",
        job_id="11111111-2222-3333-4444-555555555555",
        db_type="mssql",
        host=r"(localdb)\MSSQLLocalDB",
        port=1433,
        name="EmployeeManagementDB",
        user="sa",
        password="Pa#ss word=1",
        schema=None,
    )
    base.update(overrides)
    return PublishedDatabase(**base)


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text(
        "DB_TYPE=\n"
        "DB_CONNECTIONS=hr,adventureworks\n"
        "DB_HR_TYPE=mssql\n"
        "DB_HR_NAME=HrAutomationDb\n"
        "DB_ADVENTUREWORKS_TYPE=mssql\n"
        "DB_ADVENTUREWORKS_NAME=AdventureWorksDW2025\n",
        encoding="utf-8",
    )
    return path


def _parsed_connections(env_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[DatabaseConnectionConfig, ...]:
    """Loads the written file into the environment, as startup does, then parses it like Settings."""
    for key, value in dotenv_values(env_path).items():
        if value is not None:
            monkeypatch.setenv(key, value)
    return _parse_named_connections()


def test_appends_connection_and_extends_db_connections(env_file: Path) -> None:
    result = register_published_database(_db(), env_path=env_file)

    assert result.newly_added is True
    assert result.connection_name == "employee_management_database"
    text = env_file.read_text(encoding="utf-8")
    assert "DB_CONNECTIONS=hr,adventureworks,employee_management_database" in text
    assert "DB_HR_NAME=HrAutomationDb" in text  # existing entries are untouched


def test_written_values_round_trip_through_dotenv(env_file: Path) -> None:
    register_published_database(_db(), env_path=env_file)
    values = dotenv_values(env_file)

    prefix = "EMPLOYEE_MANAGEMENT_DATABASE"
    assert values[f"DB_{prefix}_TYPE"] == "mssql"
    assert values[f"DB_{prefix}_HOST"] == r"(localdb)\MSSQLLocalDB"
    assert values[f"DB_{prefix}_NAME"] == "EmployeeManagementDB"
    assert values[f"DB_{prefix}_USER"] == "sa"
    assert values[f"DB_{prefix}_PASSWORD"] == "Pa#ss word=1"


def test_named_instance_gets_no_port(env_file: Path) -> None:
    register_published_database(_db(port=1433), env_path=env_file)

    assert "DB_EMPLOYEE_MANAGEMENT_DATABASE_PORT" not in env_file.read_text(encoding="utf-8")


def test_tcp_host_keeps_its_port(env_file: Path) -> None:
    register_published_database(
        _db(host="db.internal.example.com", port=5432, db_type="postgresql"), env_path=env_file
    )
    values = dotenv_values(env_file)

    assert values["DB_EMPLOYEE_MANAGEMENT_DATABASE_PORT"] == "5432"


def test_written_entry_parses_as_a_real_named_connection(
    env_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    register_published_database(_db(), env_path=env_file)
    connections = {c.name: c for c in _parsed_connections(env_file, monkeypatch)}

    assert set(connections) == {"hr", "adventureworks", "employee_management_database"}
    entry = connections["employee_management_database"]
    assert entry.db_type == "mssql"
    assert entry.db_host == r"(localdb)\MSSQLLocalDB"
    assert entry.db_name == "EmployeeManagementDB"
    assert entry.db_password is not None
    assert entry.db_password.get_secret_value() == "Pa#ss word=1"


def test_same_target_is_not_registered_twice(env_file: Path) -> None:
    first = register_published_database(_db(), env_path=env_file)
    before = env_file.read_text(encoding="utf-8")

    second = register_published_database(_db(job_id="other-job"), env_path=env_file)

    assert second.newly_added is False
    assert second.connection_name == first.connection_name
    assert env_file.read_text(encoding="utf-8") == before


def test_name_collision_gets_a_numeric_suffix(env_file: Path) -> None:
    register_published_database(_db(name="OtherDb"), env_path=env_file)
    second = register_published_database(_db(name="ThirdDb"), env_path=env_file)

    assert second.connection_name == "employee_management_database_2"


def test_creates_db_connections_when_missing(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("OLLAMA_HOST=http://localhost:11434\n", encoding="utf-8")

    register_published_database(_db(), env_path=env_path)

    assert "DB_CONNECTIONS=employee_management_database" in env_path.read_text(encoding="utf-8")


def test_password_with_special_characters_survives_quoting(env_file: Path) -> None:
    tricky = 'quote"back\\slash #hash'
    register_published_database(_db(password=tricky), env_path=env_file)

    assert dotenv_values(env_file)["DB_EMPLOYEE_MANAGEMENT_DATABASE_PASSWORD"] == tricky


def test_rollback_restores_env_exactly(env_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If indexing fails after a write, the registration must disappear completely."""
    before = env_file.read_text(encoding="utf-8")
    monkeypatch.setenv("DB_CONNECTIONS", "hr,adventureworks")
    result = register_published_database(_db(), env_path=env_file)
    apply_to_runtime(result)
    assert "employee_management_database" in os.environ["DB_CONNECTIONS"]

    rollback_registration(result, env_path=env_file)

    assert env_file.read_text(encoding="utf-8") == before
    assert os.environ["DB_CONNECTIONS"] == "hr,adventureworks"
    assert "DB_EMPLOYEE_MANAGEMENT_DATABASE_NAME" not in os.environ


def test_rollback_is_a_noop_for_an_existing_registration(env_file: Path) -> None:
    register_published_database(_db(), env_path=env_file)
    second = register_published_database(_db(job_id="other"), env_path=env_file)
    before = env_file.read_text(encoding="utf-8")

    rollback_registration(second, env_path=env_file)

    assert env_file.read_text(encoding="utf-8") == before


def test_suffixed_name_when_target_name_collides(env_file: Path) -> None:
    register_published_database(_db(name="OtherDb"), env_path=env_file)

    result = register_published_database(_db(name="ThirdDb"), env_path=env_file)

    assert result.connection_name == "employee_management_database_2"
