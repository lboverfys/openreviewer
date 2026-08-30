"""GitHub App installation、组织和仓库的统一接入边界。"""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

_OWNER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_REPOSITORY_RE = re.compile(
    r"^(?P<owner>[A-Za-z0-9_.-]{1,100})/(?P<name>[A-Za-z0-9_.-]{1,100})$"
)


class GitHubAccessConfigurationError(ValueError):
    """GitHub 接入白名单缺失或包含非法值。"""


@dataclass(frozen=True, slots=True)
class GitHubAccessPolicy:
    """只允许可信 installation 和明确授权的组织或仓库。"""

    installation_ids: frozenset[int]
    organizations: frozenset[str]
    repositories: frozenset[str]

    def __post_init__(self) -> None:
        if not self.installation_ids or any(
            installation_id <= 0 for installation_id in self.installation_ids
        ):
            raise GitHubAccessConfigurationError(
                "at least one positive GitHub installation ID must be allowed"
            )
        if not self.organizations and not self.repositories:
            raise GitHubAccessConfigurationError(
                "at least one GitHub organization or repository must be allowed"
            )
        if any(not _OWNER_RE.fullmatch(owner) for owner in self.organizations):
            raise GitHubAccessConfigurationError(
                "GitHub organization allowlist contains an invalid owner"
            )
        if any(
            not _REPOSITORY_RE.fullmatch(repository)
            for repository in self.repositories
        ):
            raise GitHubAccessConfigurationError(
                "GitHub repository allowlist contains an invalid full name"
            )

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> "GitHubAccessPolicy":
        """从逗号分隔环境变量读取并严格校验白名单。"""

        values = os.environ if environment is None else environment
        raw_installations = _split_csv(
            values.get("OPENREVIEWER_GITHUB_ALLOWED_INSTALLATION_IDS", "")
        )
        try:
            installation_ids = frozenset(int(value) for value in raw_installations)
        except ValueError as exc:
            raise GitHubAccessConfigurationError(
                "GitHub installation allowlist must contain integers"
            ) from exc
        organizations = frozenset(
            value.casefold()
            for value in _split_csv(
                values.get("OPENREVIEWER_GITHUB_ALLOWED_ORGANIZATIONS", "")
            )
        )
        repositories = frozenset(
            value.casefold()
            for value in _split_csv(
                values.get("OPENREVIEWER_GITHUB_ALLOWED_REPOSITORIES", "")
            )
        )
        return cls(
            installation_ids=installation_ids,
            organizations=organizations,
            repositories=repositories,
        )

    def allows_installation(self, installation_id: int) -> bool:
        return installation_id in self.installation_ids

    def denial_reason(self, installation_id: int, repository: str) -> str | None:
        """返回稳定拒绝原因；返回 ``None`` 表示来源在授权范围内。"""

        if not self.allows_installation(installation_id):
            return "installation_not_allowed"
        match = _REPOSITORY_RE.fullmatch(repository)
        if match is None:
            return "repository_not_allowed"
        normalized_repository = repository.casefold()
        normalized_owner = match.group("owner").casefold()
        if (
            normalized_repository in self.repositories
            or normalized_owner in self.organizations
        ):
            return None
        return "repository_not_allowed"


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())
