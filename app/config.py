from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables."""

    app_env: Literal["local", "test", "production"] = "local"
    database_url: str = (
        "postgresql+psycopg://ledgerlite_app:ledgerlite-app-local@localhost:5432/"
        "ledgerlite"
    )
    database_connect_timeout_seconds: int = Field(default=5, ge=1, le=30)
    database_statement_timeout_ms: int = Field(default=10_000, ge=100, le=120_000)
    database_lock_timeout_ms: int = Field(default=3_000, ge=100, le=30_000)
    database_pool_timeout_seconds: int = Field(default=5, ge=1, le=60)
    database_pool_size: int = Field(default=5, ge=1, le=50)
    database_max_overflow: int = Field(default=5, ge=0, le=50)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # The repository's optional .env file also carries Compose-only role
    # bootstrap values. Unknown keys are ignored here while every application
    # setting above remains typed and validated.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @field_validator("database_url")
    @classmethod
    def require_sync_psycopg(cls, value: str) -> str:
        """Reject backends that cannot provide the ledger's lock guarantees."""

        try:
            driver_name = make_url(value).drivername
        except ArgumentError as exc:
            raise ValueError("DATABASE_URL must be a valid SQLAlchemy URL") from exc
        if driver_name != "postgresql+psycopg":
            raise ValueError("DATABASE_URL must use postgresql+psycopg")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
