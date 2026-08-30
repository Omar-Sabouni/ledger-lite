from __future__ import annotations

import os
from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


@pytest.fixture(scope="session")
def db_engine() -> Engine:
    """Return the same engine used by the application sessions."""
    # Keep application/database imports inside integration fixtures so schema
    # unit tests can run without initializing the database layer.
    from app import models as _models  # noqa: F401
    from app.database import SessionLocal

    engine = SessionLocal.kw.get("bind")
    if engine is None:
        raise RuntimeError("SessionLocal is not bound to a database engine")
    return engine


def _truncate_application_tables(engine: Engine) -> None:
    database_name = engine.url.database or ""
    unsafe_override = os.environ.get("LEDGERLITE_ALLOW_UNSAFE_TEST_DATABASE") == "1"
    if not database_name.endswith("_test") and not unsafe_override:
        raise RuntimeError(
            "Refusing to truncate database "
            f"{database_name!r}: test database names must end in '_test'. "
            "Set LEDGERLITE_ALLOW_UNSAFE_TEST_DATABASE=1 to override."
        )

    from app.database import Base

    tables = [
        table
        for table in Base.metadata.sorted_tables
        if table.name != "alembic_version"
    ]
    if not tables:
        raise RuntimeError(
            "No application tables are registered in SQLAlchemy metadata"
        )

    with engine.begin() as connection:
        preparer = connection.dialect.identifier_preparer
        table_list = ", ".join(preparer.format_table(table) for table in tables)
        connection.exec_driver_sql(
            f"TRUNCATE TABLE {table_list} RESTART IDENTITY CASCADE"
        )


@pytest.fixture(autouse=True)
def clean_database(request: pytest.FixtureRequest) -> Iterator[None]:
    """Give integration tests an isolated DB without touching it for unit tests."""
    if request.node.get_closest_marker("integration") is None:
        yield
        return

    db_engine: Engine = request.getfixturevalue("db_engine")
    _truncate_application_tables(db_engine)
    yield
    _truncate_application_tables(db_engine)


@pytest.fixture
def client() -> Iterator[TestClient]:
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def db_session() -> Iterator[Session]:
    from app.database import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
