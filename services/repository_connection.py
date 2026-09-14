"""复用现有 App，按页读取授权范围并验证用户选择的仓库。"""

from typing import Any

from domain.security import SafeApplicationError
from services.github import GitHubApiClient
from services.github_auth import GitHubAppTokenProvider


class RepositoryConnector:
    def __init__(self, api: GitHubApiClient, tokens: GitHubAppTokenProvider) -> None:
        self.api, self.tokens = api, tokens

    def installations(self, page: int = 1) -> dict[str, Any]:
        app = self.tokens.app_request("/app")
        result = self.tokens.app_request("/app/installations", {"per_page": 30, "page": page})
        if not isinstance(app, dict) or not isinstance(result, list):
            raise ValueError("GitHub App 授权信息无法读取")
        return {"app_name": app.get("name"), "authorize_url": f"https://github.com/apps/{app['slug']}/installations/new",
                "items": [{"id": item["id"], "account": item["account"]["login"],
                           "selection": item.get("repository_selection"),
                           "manage_url": item["html_url"]} for item in result],
                "has_more": len(result) == 30}

    def _installation(self, installation_id: int) -> dict[str, Any]:
        # App JWT 接口只返回属于当前 App 的安装，不信任网页传来的 installation ID。
        item = self.tokens.app_request(f"/app/installations/{installation_id}")
        if not isinstance(item, dict) or item.get("app_id") != self.tokens.app_id or item.get("suspended_at"):
            raise ValueError("该授权不属于当前 App，或已被 GitHub 暂停")
        return item

    def repositories(self, installation_id: int, page: int = 1) -> dict[str, Any]:
        installation = self._installation(installation_id)
        token = self.tokens.get_token(installation_id)
        result = self.api.request_json("GET", "/installation/repositories", bearer_token=token,
            params={"per_page": 30, "page": page}).payload
        if not isinstance(result, dict) or not isinstance(result.get("repositories"), list):
            raise ValueError("GitHub 仓库列表无法读取")
        return {"items": [{"id": item["id"], "repository": item["full_name"]}
                          for item in result["repositories"]],
                "manage_url": installation["html_url"],
                "selection": installation.get("repository_selection"),
                "has_more": page * 30 < result.get("total_count", 0)}

    def check(self, installation_id: int, repository: str) -> int:
        try:
            installation = self._installation(installation_id)
            permissions = installation.get("permissions", {})
            if any(permissions.get(name) not in {"read", "write"} for name in ("contents", "pull_requests", "checks", "statuses")):
                raise ValueError("当前 App 缺少读取代码、PR 或 CI 的权限，请在同一个 App 中补充权限")
            if "pull_request" not in installation.get("events", []):
                raise ValueError("当前 App 未订阅 pull_request 事件，无法自动接收新 PR")
            token = self.tokens.get_token(installation_id)
            result = self.api.request_json("GET", f"/repos/{repository}", bearer_token=token).payload
            if not isinstance(result, dict) or str(result.get("full_name", "")).casefold() != repository.casefold():
                raise ValueError("GitHub 返回的仓库与所选仓库不一致")
            if result.get("archived") or result.get("disabled"):
                raise ValueError("仓库已归档或停用，无法启用审查")
            return int(result["id"])
        except SafeApplicationError as exc:
            raise ValueError("无法访问这个仓库，请给现有 App 补充该仓库授权后重试") from exc
