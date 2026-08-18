"""SQLAlchemy engine configuration without hard-coded credentials."""

from collections.abc import Mapping
from dataclasses import dataclass
import os

from sqlalchemy import URL, Engine, create_engine, make_url
from sqlalchemy.orm import Session, sessionmaker


class DatabaseConfigurationError(RuntimeError):
    """Required database configuration is missing or invalid."""


def database_url_from_environment(
    environment: Mapping[str, str] | None = None,
) -> URL:
    values = os.environ if environment is None else environment
    explicit_url = values.get("OPENREVIEWER_DATABASE_URL", "").strip()
    if explicit_url:
        try:
            return make_url(explicit_url)
        except Exception as exc:  # SQLAlchemy exposes multiple parse errors.
            raise DatabaseConfigurationError(
                "OPENREVIEWER_DATABASE_URL is invalid"
            ) from exc

    password = values.get("OPENREVIEWER_DB_PASSWORD")
    if password is None or not password:
        raise DatabaseConfigurationError(
            "OPENREVIEWER_DB_PASSWORD is required"
        )

    port_value = values.get("OPENREVIEWER_DB_PORT", "5432")
    try:
        port = int(port_value)
    except ValueError as exc:
        raise DatabaseConfigurationError(
            "OPENREVIEWER_DB_PORT must be an integer"
        ) from exc
    if not 1 <= port <= 65535:
        raise DatabaseConfigurationError(
            "OPENREVIEWER_DB_PORT must be between 1 and 65535"
        )

    return URL.create(
        drivername="postgresql+psycopg",
        username=values.get("OPENREVIEWER_DB_USER", "openreviewer"),
        password=password,
        host=values.get("OPENREVIEWER_DB_HOST", "postgres"),
        port=port,
        database=values.get("OPENREVIEWER_DB_NAME", "openreviewer"),
    )


@dataclass(slots=True)
class Database:
    engine: Engine
    sessions: sessionmaker[Session]

    @classmethod
    def connect(cls, url: str | URL) -> "Database":
        engine = create_engine(url, pool_pre_ping=True)
        return cls(
            engine=engine,
            sessions=sessionmaker(
                bind=engine,
                class_=Session,
                expire_on_commit=False,
            ),
        )

    @classmethod
    def from_environment(cls) -> "Database":
        return cls.connect(database_url_from_environment())

    def dispose(self) -> None:
        self.engine.dispose()
