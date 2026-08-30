"""OpenReviewer 管理界面的角色、权限和资源范围。"""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256

_OWNER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_REPOSITORY_RE = re.compile(
    r"^(?P<owner>[A-Za-z0-9_.-]{1,100})/(?P<name>[A-Za-z0-9_.-]{1,100})$"
)


class AccessRole(StrEnum):
    VIEWER = "viewer"
    ADJUDICATOR = "adjudicator"
    PUBLISHER = "publisher"
    ADMINISTRATOR = "administrator"


class Permission(StrEnum):
    VIEW_REVIEWS = "reviews:view"
    ADJUDICATE_FINDINGS = "findings:adjudicate"
    APPROVE_REVIEWS = "reviews:approve"
    PUBLISH_REVIEWS = "reviews:publish"
    MANAGE_REVIEWS = "reviews:manage"
    MANAGE_SETTINGS = "settings:manage"
    MANAGE_KNOWLEDGE = "knowledge:manage"


@dataclass(frozen=True, slots=True)
class ResourceScope:
    """一个账号可以访问的 GitHub 资源范围。

    ``installation_ids`` 为空表示不按 installation 限制；组织和仓库都为空时，
    只要 installation 命中就允许该 installation 下的全部仓库。三个集合全部为空
    且 ``unrestricted`` 为 ``False`` 时表示“拒绝全部”，用于显式的空权限配置。
    管理员使用 ``unrestricted_scope``，这样查询层可以明确区分全量访问和空范围。
    """

    installation_ids: frozenset[int] = frozenset()
    organizations: frozenset[str] = frozenset()
    repositories: frozenset[str] = frozenset()
    unrestricted: bool = False

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
            for value in self.installation_ids
        ):
            raise ValueError("resource scope installation IDs must be positive integers")
        if any(
            not isinstance(value, str)
            or _OWNER_RE.fullmatch(value.strip()) is None
            for value in self.organizations
        ):
            raise ValueError("resource scope contains an invalid organization")
        if any(
            not isinstance(value, str)
            or _REPOSITORY_RE.fullmatch(value.strip()) is None
            for value in self.repositories
        ):
            raise ValueError("resource scope contains an invalid repository")
        # GitHub 的 owner/repository 比较不区分大小写。把集合在构造时就规范化，
        # 让内存判断、SSE 缓存键和数据库里的 repository_key 使用同一套值，
        # 也避免每次 ``allows`` 都重新构造临时集合。
        object.__setattr__(
            self,
            "organizations",
            frozenset(value.strip().casefold() for value in self.organizations),
        )
        object.__setattr__(
            self,
            "repositories",
            frozenset(value.strip().casefold() for value in self.repositories),
        )
        if self.unrestricted and (
            self.installation_ids or self.organizations or self.repositories
        ):
            raise ValueError("unrestricted resource scope cannot contain selectors")

    @classmethod
    def unrestricted_scope(cls) -> "ResourceScope":
        """返回不受资源过滤的范围（仅管理员默认使用）。"""

        return cls(unrestricted=True)

    @classmethod
    def deny_all(cls) -> "ResourceScope":
        """返回显式拒绝全部资源的范围（非管理员缺省使用）。"""

        return cls()

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "ResourceScope":
        """严格解析额外账号 JSON 中的 ``scope`` 对象。"""

        allowed = {"installation_ids", "organizations", "repositories"}
        if set(value) != allowed:
            raise ValueError(
                "resource scope must contain installation_ids, organizations and repositories"
            )

        raw_installations = value["installation_ids"]
        raw_organizations = value["organizations"]
        raw_repositories = value["repositories"]
        if not (
            isinstance(raw_installations, Sequence)
            and not isinstance(raw_installations, (str, bytes, bytearray))
            and isinstance(raw_organizations, Sequence)
            and not isinstance(raw_organizations, (str, bytes, bytearray))
            and isinstance(raw_repositories, Sequence)
            and not isinstance(raw_repositories, (str, bytes, bytearray))
        ):
            raise ValueError("resource scope selectors must be arrays")
        if any(len(items) > 1000 for items in (raw_installations, raw_organizations, raw_repositories)):
            raise ValueError("resource scope selectors contain too many entries")

        installation_ids: set[int] = set()
        for item in raw_installations:
            if not isinstance(item, int) or isinstance(item, bool) or item <= 0:
                raise ValueError("resource scope installation IDs must be positive integers")
            installation_ids.add(item)

        organizations: set[str] = set()
        for item in raw_organizations:
            if not isinstance(item, str) or _OWNER_RE.fullmatch(item.strip()) is None:
                raise ValueError("resource scope contains an invalid organization")
            organizations.add(item.strip())

        repositories: set[str] = set()
        for item in raw_repositories:
            if not isinstance(item, str) or _REPOSITORY_RE.fullmatch(item.strip()) is None:
                raise ValueError("resource scope contains an invalid repository")
            repositories.add(item.strip())

        return cls(
            installation_ids=frozenset(installation_ids),
            organizations=frozenset(organizations),
            repositories=frozenset(repositories),
        )

    def allows(self, installation_id: int, repository: str) -> bool:
        """判断一个任务资源是否在范围内。"""

        if self.unrestricted:
            return True
        if self.installation_ids and installation_id not in self.installation_ids:
            return False
        if not self.installation_ids and not self.organizations and not self.repositories:
            return False
        if not self.organizations and not self.repositories:
            return True
        match = _REPOSITORY_RE.fullmatch(repository.strip())
        if match is None:
            return False
        normalized_repository = repository.strip().casefold()
        normalized_owner = match.group("owner").casefold()
        return normalized_repository in self.repositories or normalized_owner in self.organizations

    @property
    def cache_key(self) -> str:
        """返回不含原始用户名或仓库文本的稳定 SSE 缓存键。"""

        if self.unrestricted:
            return "all"
        payload = {
            "installation_ids": sorted(self.installation_ids),
            "organizations": sorted(self.organizations),
            "repositories": sorted(self.repositories),
        }
        return sha256(
            json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
                "utf-8"
            )
        ).hexdigest()[:32]


_ROLE_PERMISSIONS: dict[AccessRole, frozenset[Permission]] = {
    AccessRole.VIEWER: frozenset({Permission.VIEW_REVIEWS}),
    AccessRole.ADJUDICATOR: frozenset(
        {
            Permission.VIEW_REVIEWS,
            Permission.ADJUDICATE_FINDINGS,
            Permission.APPROVE_REVIEWS,
        }
    ),
    AccessRole.PUBLISHER: frozenset(
        {Permission.VIEW_REVIEWS, Permission.PUBLISH_REVIEWS}
    ),
    AccessRole.ADMINISTRATOR: frozenset(Permission),
}


def permissions_for(role: AccessRole) -> frozenset[Permission]:
    return _ROLE_PERMISSIONS[role]


def has_permission(role: AccessRole, permission: Permission) -> bool:
    return permission in permissions_for(role)
