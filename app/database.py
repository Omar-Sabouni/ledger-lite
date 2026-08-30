from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()

# Fail quickly when PostgreSQL is unavailable or a statement is stuck waiting on
# a lock.  ``hide_parameters`` is important for a financial system: SQL values
# must never be copied into exception logs by the engine.
engine = create_engine(
    settings.database_url,
    connect_args={
        "connect_timeout": settings.database_connect_timeout_seconds,
        "options": (
            f"-c statement_timeout={settings.database_statement_timeout_ms} "
            f"-c lock_timeout={settings.database_lock_timeout_ms}"
        ),
    },
    hide_parameters=True,
    isolation_level="READ COMMITTED",
    max_overflow=settings.database_max_overflow,
    pool_pre_ping=True,
    pool_size=settings.database_pool_size,
    pool_timeout=settings.database_pool_timeout_seconds,
)
SessionLocal = sessionmaker(
    bind=engine,
    class_=Session,
    autoflush=False,
    expire_on_commit=False,
)


def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
