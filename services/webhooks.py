"""只入库、不执行外部副作用的 GitHub Webhook 验签接入。"""

import hmac
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from domain.enums import ExecutionStatus, PullRequestAction
from domain.models import PullRequestWebhook
from domain.security import ErrorCode, SafeApplicationError, SafeError
from services.github_access import GitHubAccessPolicy


class WebhookConfigurationError(RuntimeError):
    """Webhook 密钥或请求大小配置不可用。"""


class WebhookDeliveryConflictError(SafeApplicationError):
    """同一个投递 ID 被复用于不同的已签名内容。"""


class WebhookPersistenceError(SafeApplicationError):
    """已验签投递无法原子持久化。"""


class WebhookRequestError(SafeApplicationError):
    """已签名请求在有界读取请求体后未通过校验。"""


@dataclass(frozen=True, slots=True)
class GitHubWebhookSettings:
    secret: bytes = field(repr=False)
    previous_secrets: tuple[bytes, ...] = field(default=(), repr=False)
    max_body_bytes: int = 256 * 1024

    def __post_init__(self) -> None:
        if len(self.secret) < 32:
            raise WebhookConfigurationError(
                "the GitHub webhook secret must contain at least 32 bytes"
            )
        secrets = (self.secret, *self.previous_secrets)
        if len(self.previous_secrets) > 4:
            raise WebhookConfigurationError(
                "at most four previous GitHub webhook secrets are supported"
            )
        if any(len(secret) < 32 for secret in secrets):
            raise WebhookConfigurationError(
                "GitHub webhook secrets must contain at least 32 bytes"
            )
        if len(secrets) != len(set(secrets)):
            raise WebhookConfigurationError("GitHub webhook secrets must be unique")
        if not 1024 <= self.max_body_bytes <= 256 * 1024:
            raise WebhookConfigurationError(
                "the GitHub webhook body limit must be between 1 KiB and 256 KiB"
            )

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> "GitHubWebhookSettings":
        values = os.environ if environment is None else environment
        direct_secret = values.get("OPENREVIEWER_GITHUB_WEBHOOK_SECRET", "")
        secret_file = values.get(
            "OPENREVIEWER_GITHUB_WEBHOOK_SECRET_FILE", ""
        ).strip()
        if direct_secret and secret_file:
            raise WebhookConfigurationError(
                "configure only one GitHub webhook secret source"
            )
        if secret_file:
            try:
                secret = Path(secret_file).read_bytes().strip()
            except OSError as exc:
                raise WebhookConfigurationError(
                    "the GitHub webhook secret file could not be read"
                ) from exc
        else:
            secret = direct_secret.encode("utf-8")
        previous_secrets = _read_previous_secrets(values)
        try:
            body_limit = int(
                values.get("OPENREVIEWER_GITHUB_WEBHOOK_MAX_BYTES", "262144")
            )
        except ValueError as exc:
            raise WebhookConfigurationError(
                "the GitHub webhook body limit must be an integer"
            ) from exc
        return cls(
            secret=secret,
            previous_secrets=previous_secrets,
            max_body_bytes=body_limit,
        )

    @property
    def verification_secrets(self) -> tuple[bytes, ...]:
        return (self.secret, *self.previous_secrets)


@dataclass(frozen=True, slots=True)
class WebhookSubmissionResult:
    delivery_id: str
    review_run_id: str
    review_task_id: str
    review_version_key: str
    execution_status: ExecutionStatus
    accepted_at: datetime
    created: bool


@dataclass(frozen=True, slots=True)
class WebhookReceipt:
    accepted: bool
    delivery_id: str
    created: bool = False
    reason: str | None = None
    submission: WebhookSubmissionResult | None = None


class GitHubWebhookRepository(Protocol):
    def create_or_get(
        self,
        event: PullRequestWebhook,
        payload_sha256: str,
    ) -> WebhookSubmissionResult:
        """原子保存投递、版本、运行、任务和 Outbox 记录。"""
        ...


class _InstallationPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int = Field(gt=0)


class _RepositoryPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int = Field(gt=0)
    full_name: str = Field(
        min_length=3,
        max_length=255,
        pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
    )


class _HeadPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    sha: str = Field(min_length=40, max_length=64)


class _PullRequestPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    number: int = Field(gt=0)
    head: _HeadPayload


class _AcceptedPullRequestPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    action: PullRequestAction
    installation: _InstallationPayload
    repository: _RepositoryPayload
    pull_request: _PullRequestPayload


_DELIVERY_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")
_SIGNATURE_RE = re.compile(r"^sha256=([0-9a-fA-F]{64})$")
_SUPPORTED_ACTIONS = frozenset(member.value for member in PullRequestAction)


class GitHubWebhookService:
    """校验原始字节、过滤事件，并把可信数据交给持久化层。"""

    def __init__(
        self,
        repository: GitHubWebhookRepository,
        settings: GitHubWebhookSettings,
        access_policy: GitHubAccessPolicy,
    ) -> None:
        self._repository = repository
        self.settings = settings
        self.access_policy = access_policy

    def receive(
        self,
        *,
        event_name: str,
        delivery_id: str,
        signature: str,
        body: bytes,
    ) -> WebhookReceipt:
        self._verify_signature(body, signature)
        if not _DELIVERY_ID_RE.fullmatch(delivery_id):
            raise self._invalid_payload("GitHub delivery ID 格式无效")
        if event_name != "pull_request":
            return WebhookReceipt(
                accepted=False,
                delivery_id=delivery_id,
                reason="unsupported_event",
            )

        payload = self._decode_object(body)
        action = payload.get("action")
        if not isinstance(action, str):
            raise self._invalid_payload("GitHub PR 事件缺少合法 action")
        if action not in _SUPPORTED_ACTIONS:
            return WebhookReceipt(
                accepted=False,
                delivery_id=delivery_id,
                reason="unsupported_action",
            )
        try:
            accepted = _AcceptedPullRequestPayload.model_validate(payload)
            denial_reason = self.access_policy.denial_reason(
                accepted.installation.id,
                accepted.repository.full_name,
            )
            if denial_reason is not None:
                return WebhookReceipt(
                    accepted=False,
                    delivery_id=delivery_id,
                    reason=denial_reason,
                )
            event = PullRequestWebhook(
                action=accepted.action,
                delivery_id=delivery_id,
                installation_id=accepted.installation.id,
                repository_id=accepted.repository.id,
                repository=accepted.repository.full_name,
                pull_request_number=accepted.pull_request.number,
                head_sha=accepted.pull_request.head.sha,
            )
        except ValidationError as exc:
            raise self._invalid_payload("GitHub PR 事件缺少必需字段") from exc

        result = self._repository.create_or_get(event, sha256(body).hexdigest())
        return WebhookReceipt(
            accepted=True,
            delivery_id=delivery_id,
            created=result.created,
            submission=result,
        )

    def _verify_signature(self, body: bytes, signature: str) -> None:
        match = _SIGNATURE_RE.fullmatch(signature)
        supplied = match.group(1).lower() if match is not None else "0" * 64
        signature_valid = False
        for secret in self.settings.verification_secrets:
            expected = hmac.new(secret, body, sha256).hexdigest()
            signature_valid = hmac.compare_digest(supplied, expected) or signature_valid
        if match is None or not signature_valid:
            raise WebhookRequestError(
                SafeError(
                    code=ErrorCode.WEBHOOK_INVALID_SIGNATURE,
                    safe_message="GitHub Webhook 签名校验失败",
                    retryable=False,
                )
            )

    @staticmethod
    def _decode_object(body: bytes) -> dict[str, object]:
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubWebhookService._invalid_payload(
                "GitHub Webhook 请求体不是合法 JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise GitHubWebhookService._invalid_payload(
                "GitHub Webhook 请求体必须是 JSON 对象"
            )
        return payload

    @staticmethod
    def _invalid_payload(message: str) -> WebhookRequestError:
        return WebhookRequestError(
            SafeError(
                code=ErrorCode.WEBHOOK_INVALID_PAYLOAD,
                safe_message=message,
                retryable=False,
            )
        )


def _read_previous_secrets(values: Mapping[str, str]) -> tuple[bytes, ...]:
    direct = values.get("OPENREVIEWER_GITHUB_WEBHOOK_PREVIOUS_SECRETS_JSON", "")
    file_name = values.get(
        "OPENREVIEWER_GITHUB_WEBHOOK_PREVIOUS_SECRETS_FILE", ""
    ).strip()
    if direct and file_name:
        raise WebhookConfigurationError(
            "configure only one previous GitHub webhook secret source"
        )
    if file_name:
        try:
            raw = Path(file_name).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise WebhookConfigurationError(
                "the previous GitHub webhook secret file could not be read"
            ) from exc
    else:
        raw = direct.strip()
    if not raw:
        return ()
    if len(raw.encode("utf-8")) > 16 * 1024:
        raise WebhookConfigurationError(
            "the previous GitHub webhook secret list is too large"
        )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WebhookConfigurationError(
            "previous GitHub webhook secrets must be a JSON array"
        ) from exc
    if not isinstance(payload, list) or len(payload) > 4:
        raise WebhookConfigurationError(
            "previous GitHub webhook secrets must contain at most four entries"
        )
    secrets: list[bytes] = []
    for value in payload:
        if not isinstance(value, str):
            raise WebhookConfigurationError(
                "previous GitHub webhook secret entries must be strings"
            )
        encoded = value.strip().encode("utf-8")
        if len(encoded) < 32:
            raise WebhookConfigurationError(
                "previous GitHub webhook secrets must contain at least 32 bytes"
            )
        secrets.append(encoded)
    if len(secrets) != len(set(secrets)):
        raise WebhookConfigurationError(
            "previous GitHub webhook secrets must be unique"
        )
    return tuple(secrets)
