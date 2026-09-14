from unittest.mock import Mock

import pytest

from services.repository_connection import RepositoryConnector


def connection(selection="selected"):
    installation = {"id": 10, "app_id": 1, "account": {"login": "owner"},
        "html_url": "https://github.com/settings/installations/10", "repository_selection": selection,
        "permissions": {name: "read" for name in ("contents", "pull_requests", "checks", "statuses")},
        "events": ["pull_request"]}
    tokens, api = Mock(app_id=1), Mock()
    tokens.app_request.side_effect = lambda path, params=None: (
        {"name": "Existing App", "slug": "existing-app"} if path == "/app" else
        [installation] if path == "/app/installations" else installation)
    tokens.get_token.return_value = "offline-token"
    api.request_json.return_value.payload = {"id": 42, "full_name": "owner/repo"}
    return RepositoryConnector(api, tokens), installation


def test_existing_app_lists_installations_and_selected_repositories():
    service, _ = connection()
    assert service.installations()["authorize_url"] == "https://github.com/apps/existing-app/installations/new"
    service.api.request_json.return_value.payload = {"total_count": 31, "repositories": [{"id": 42, "full_name": "owner/repo"}]}
    listed = service.repositories(10)
    assert listed["items"][0]["repository"] == "owner/repo" and listed["has_more"]
    assert listed["manage_url"].endswith("/10")


def test_all_repository_authorization_is_reused_and_check_requires_pr_events():
    service, installation = connection("all")
    assert service.check(10, "owner/repo") == 42
    installation["events"] = []
    with pytest.raises(ValueError, match="pull_request"):
        service.check(10, "owner/repo")
    installation["events"] = ["pull_request"]
    installation["app_id"] = 99
    with pytest.raises(ValueError, match="不属于"):
        service.check(10, "owner/repo")
