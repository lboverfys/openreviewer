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
        assert str(exc) == "OPENREVIEWER_DB_PASSWORD is required"
    else:
        raise AssertionError("missing database password should fail")
