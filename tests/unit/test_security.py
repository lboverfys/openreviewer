from io import StringIO
import logging
from pathlib import Path

import pytest

from domain.paths import (
    RepositoryPathError,
    normalize_repository_path,
    resolve_repository_path,
)
from domain.security import (
    REDACTED,
    TRUNCATED,
    ErrorCode,
    RedactingLogFilter,
    SafeError,
    redact_sensitive,
    redact_text,
)


FAKE_GITHUB_TOKEN = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"
FAKE_MODEL_KEY = "sk-abcdefghijklmnopqrstuvwxyz123456"


def test_free_text_and_nested_values_are_redacted() -> None:
    source = (
        f"Authorization: Bearer {FAKE_GITHUB_TOKEN} "
        f"url=https://admin:secret-value@example.test/repo key={FAKE_MODEL_KEY}"
    )
    nested = {
        "request": source,
        "credentials": {"username": "admin", "password": "plain-password"},
        "metadata": {"password": "plain-password", "token_count": 42},
    }

    sanitized_text = redact_text(source)
    sanitized = redact_sensitive(nested)

    assert FAKE_GITHUB_TOKEN not in sanitized_text
    assert FAKE_MODEL_KEY not in sanitized_text
    assert "secret-value" not in sanitized_text
    assert sanitized["credentials"] == REDACTED
    assert sanitized["metadata"]["password"] == REDACTED
    assert sanitized["metadata"]["token_count"] == 42


def test_safe_error_and_log_filter_share_the_redaction_policy() -> None:
    error = SafeError(
        code=ErrorCode.WORKER_UNEXPECTED_ERROR,
        safe_message=f"request failed token={FAKE_GITHUB_TOKEN}",
        retryable=True,
        details={"authorization": f"Bearer {FAKE_GITHUB_TOKEN}"},
    )
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactingLogFilter())
    logger = logging.getLogger("openreviewer.security-test")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.ERROR)

    try:
        raise RuntimeError(f"password=plain-password {FAKE_GITHUB_TOKEN}")
    except RuntimeError:
        logger.exception("failed with %s", FAKE_MODEL_KEY)

    rendered = stream.getvalue()
    assert FAKE_GITHUB_TOKEN not in error.safe_message
    assert FAKE_GITHUB_TOKEN not in str(error.details)
    assert FAKE_GITHUB_TOKEN not in rendered
    assert FAKE_MODEL_KEY not in rendered
    assert "plain-password" not in rendered
    assert REDACTED in rendered


def test_unknown_exception_text_is_not_exposed_or_persisted() -> None:
    private_text = "customer-specific-value-that-is-not-a-known-token-format"

    error = SafeError.from_exception(RuntimeError(private_text))

    assert error.code is ErrorCode.WORKER_UNEXPECTED_ERROR
    assert error.details == {"exception_type": "RuntimeError"}
    assert private_text not in str(error.public_payload())


def test_recursive_redaction_has_a_shared_output_budget() -> None:
    oversized = {
        f"field-{index}": "x" * 4000
        for index in range(1000)
    }

    sanitized = redact_sensitive(oversized)

    assert TRUNCATED in str(sanitized)
    assert len(str(sanitized)) < 50_000


@pytest.mark.parametrize(
    "value",
    [
        "/etc/passwd",
        "C:\\Windows\\system.ini",
        "D:/secrets.txt",
        "\\\\server\\share\\secret.txt",
        "../secret.txt",
        "src/../../secret.txt",
        "src//main.py",
        "src/./main.py",
        "src/secret\x00.txt",
    ],
)
def test_repository_path_rejects_absolute_and_traversal_forms(value: str) -> None:
    with pytest.raises(RepositoryPathError):
        normalize_repository_path(value)


def test_repository_path_normalizes_a_valid_windows_separator() -> None:
    assert normalize_repository_path("apps\\api\\main.py") == "apps/api/main.py"


def test_resolved_repository_path_blocks_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "inside.txt").write_text("inside", encoding="utf-8")
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    link = root / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"当前环境不能创建测试符号链接: {exc}")

    assert resolve_repository_path(root, "inside.txt") == (root / "inside.txt").resolve()
    with pytest.raises(RepositoryPathError):
        resolve_repository_path(root, "linked/secret.txt")
