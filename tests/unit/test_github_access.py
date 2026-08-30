import pytest

from services.github_access import (
    GitHubAccessConfigurationError,
    GitHubAccessPolicy,
)


def test_policy_allows_explicit_repository_or_whole_organization() -> None:
    policy = GitHubAccessPolicy.from_environment(
        {
            "OPENREVIEWER_GITHUB_ALLOWED_INSTALLATION_IDS": "10, 20",
            "OPENREVIEWER_GITHUB_ALLOWED_ORGANIZATIONS": "Trusted-Org",
            "OPENREVIEWER_GITHUB_ALLOWED_REPOSITORIES": "exception/single-repo",
        }
    )

    assert policy.denial_reason(10, "trusted-org/any-repo") is None
    assert policy.denial_reason(20, "EXCEPTION/SINGLE-REPO") is None
    assert policy.denial_reason(30, "trusted-org/any-repo") == (
        "installation_not_allowed"
    )
    assert policy.denial_reason(10, "outside/not-listed") == (
        "repository_not_allowed"
    )


@pytest.mark.parametrize(
    "environment",
    [
        {},
        {
            "OPENREVIEWER_GITHUB_ALLOWED_INSTALLATION_IDS": "not-an-id",
            "OPENREVIEWER_GITHUB_ALLOWED_REPOSITORIES": "owner/repo",
        },
        {
            "OPENREVIEWER_GITHUB_ALLOWED_INSTALLATION_IDS": "10",
        },
        {
            "OPENREVIEWER_GITHUB_ALLOWED_INSTALLATION_IDS": "10",
            "OPENREVIEWER_GITHUB_ALLOWED_REPOSITORIES": "not-a-full-name",
        },
    ],
)
def test_policy_configuration_fails_closed(
    environment: dict[str, str],
) -> None:
    with pytest.raises(GitHubAccessConfigurationError):
        GitHubAccessPolicy.from_environment(environment)
