"""Identifiers used to make review runs stable and replayable."""

import re


_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40,64}$")


def normalize_sha(value: str) -> str:
    """Return a canonical lowercase full Git object SHA."""

    normalized = value.strip().lower()
    if not _SHA_PATTERN.fullmatch(normalized):
        raise ValueError("SHA must contain 40 to 64 hexadecimal characters")
    return normalized


def build_review_version_key(
    repository_id: int,
    pull_request_number: int,
    head_sha: str,
) -> str:
    """Build the stable identity for one PR revision."""

    if repository_id <= 0:
        raise ValueError("repository_id must be positive")
    if pull_request_number <= 0:
        raise ValueError("pull_request_number must be positive")
    return f"{repository_id}:{pull_request_number}:{normalize_sha(head_sha)}"


def build_thread_id(
    repository_id: int,
    pull_request_number: int,
    head_sha: str,
    review_run_id: str,
) -> str:
    """Build the LangGraph thread identity for one review execution."""

    run_id = review_run_id.strip()
    if not run_id:
        raise ValueError("review_run_id must not be empty")
    return f"{build_review_version_key(repository_id, pull_request_number, head_sha)}:{run_id}"
