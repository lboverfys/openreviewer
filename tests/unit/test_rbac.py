import pytest

from services.rbac import (
    AccessRole,
    Permission,
    ResourceScope,
    has_permission,
    permissions_for,
)


def test_role_permission_matrix_is_minimal_and_explicit() -> None:
    expected = {
        AccessRole.VIEWER: {Permission.VIEW_REVIEWS},
        AccessRole.ADJUDICATOR: {
            Permission.VIEW_REVIEWS,
            Permission.ADJUDICATE_FINDINGS,
            Permission.APPROVE_REVIEWS,
        },
        AccessRole.PUBLISHER: {
            Permission.VIEW_REVIEWS,
            Permission.PUBLISH_REVIEWS,
        },
        AccessRole.ADMINISTRATOR: set(Permission),
    }

    for role, role_permissions in expected.items():
        assert permissions_for(role) == frozenset(role_permissions)
        for permission in Permission:
            assert has_permission(role, permission) is (
                permission in role_permissions
            )


def test_non_administrator_roles_never_receive_management_permissions() -> None:
    privileged = {
        Permission.MANAGE_REVIEWS,
        Permission.MANAGE_SETTINGS,
        Permission.MANAGE_KNOWLEDGE,
    }

    for role in (
        AccessRole.VIEWER,
        AccessRole.ADJUDICATOR,
        AccessRole.PUBLISHER,
    ):
        assert permissions_for(role).isdisjoint(privileged)


def test_resource_scope_matches_installation_and_repository_selectors() -> None:
    scope = ResourceScope.from_mapping(
        {
            "installation_ids": [10],
            "organizations": ["lboverfys"],
            "repositories": ["other/allowed"],
        }
    )

    assert scope.allows(10, "lboverfys/NiuMa") is True
    assert scope.allows(10, "other/allowed") is True
    assert scope.allows(11, "lboverfys/NiuMa") is False
    assert scope.allows(10, "other/secret") is False
    assert scope.cache_key == ResourceScope.from_mapping(
        {
            "installation_ids": [10],
            "organizations": ["LBOverfys"],
            "repositories": ["OTHER/ALLOWED"],
        }
    ).cache_key


def test_resource_scope_rejects_ambiguous_or_empty_configuration() -> None:
    with pytest.raises(ValueError, match="must contain"):
        ResourceScope.from_mapping({"repositories": ["owner/repo"]})

    empty = ResourceScope.from_mapping(
        {
            "installation_ids": [],
            "organizations": [],
            "repositories": [],
        }
    )
    assert empty.allows(10, "owner/repo") is False

    with pytest.raises(ValueError, match="invalid repository"):
        ResourceScope.from_mapping(
            {
                "installation_ids": [10],
                "organizations": [],
                "repositories": ["owner/repo/extra"],
            }
        )
