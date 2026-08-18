from pathlib import Path

from sqlalchemy import URL

from persistence.database import (
    DatabaseConfigurationError,
    database_url_from_environment,
)


def test_database_url_uses_separate_fields_without_manual_url_encoding() -> None:
    url = database_url_from_environment(
        {
            "OPENREVIEWER_DB_HOST": "postgres.internal",
            "OPENREVIEWER_DB_PORT": "5433",
            "OPENREVIEWER_DB_NAME": "reviews",
            "OPENREVIEWER_DB_USER": "review_user",
            "OPENREVIEWER_DB_PASSWORD": "colon:@/password",
        }
    )

    assert isinstance(url, URL)
    assert url.drivername == "postgresql+psycopg"
    assert url.host == "postgres.internal"
    assert url.port == 5433
    assert url.database == "reviews"
    assert url.username == "review_user"
    assert url.password == "colon:@/password"


def test_database_url_requires_a_password() -> None:
    try:
        database_url_from_environment({})
    except DatabaseConfigurationError as exc:
        assert str(exc) == (
            "OPENREVIEWER_DB_PASSWORD or OPENREVIEWER_DB_PASSWORD_FILE is required"
        )
    else:
        raise AssertionError("missing database password should fail")


def test_database_password_can_be_read_from_a_secret_file(tmp_path: Path) -> None:
    password_file = tmp_path / "postgres-password"
    password_file.write_text("file-only-password\n", encoding="utf-8")

    url = database_url_from_environment(
        {
            "OPENREVIEWER_DB_HOST": "postgres",
            "OPENREVIEWER_DB_PASSWORD_FILE": str(password_file),
        }
    )

    assert url.password == "file-only-password"
