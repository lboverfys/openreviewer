"""检查外发前的阻止行为，不调用真实供应商。"""

import json
from unittest.mock import Mock

import httpx
import pytest

from domain.egress import EgressPolicy
from domain.enums import ModelProvider
from domain.security import ErrorCode, SafeApplicationError
from services.egress import (
    check_paths,
    check_payload,
    check_texts,
    egress_scope,
    review_egress,
)
from services.model_budget import model_budget_scope
from services.model_providers import create_model_reviewer
from services.model_review import ModelServiceSettings
from tests.unit.test_model_review import make_model_input


@pytest.mark.parametrize("path", [".env", "service/.env.prod", "keys/signing.pem", "keys/api.KEY"])
def test_private_paths_are_blocked_without_echoing_content(path):
    events = []
    with egress_scope(EgressPolicy(), events.append), pytest.raises(SafeApplicationError) as caught:
        check_paths(("src/Service.java", path))
    assert caught.value.error.code == ErrorCode.MODEL_EGRESS_DENIED
    assert events == [{"egress_reason": "blocked_path"}]
    assert path not in str(events)


@pytest.mark.parametrize("text", ["ghp_" + "a" * 36, "sk-" + "b" * 28,
    "-----BEGIN PRIVATE KEY-----\nprivate\n-----END PRIVATE KEY-----",
    'api_key="12345678901234567890"', "postgres://user:private@localhost/db"])
def test_secrets_are_detected_inside_structured_prompt_and_plain_retrieval(text):
    with egress_scope(EgressPolicy()):
        with pytest.raises(SafeApplicationError):
            check_payload("https://api.openai.com", {"input": json.dumps({"patch": text})})
        with pytest.raises(SafeApplicationError):
            check_texts((text,))


def test_host_match_is_exact_and_scope_does_not_leak():
    with egress_scope(EgressPolicy(allowed_hosts=("api.openai.com",))):
        check_payload("https://api.openai.com/v1", {"code": "token = request.token"})
        with review_egress(EgressPolicy()):
            with pytest.raises(SafeApplicationError):
                check_payload("https://api.openai.com.example.org", {})
    check_payload("https://example.org", {})


@pytest.mark.parametrize("policy", [dict(blocked_paths=("../secret",)), dict(blocked_paths=("/etc/config",)),
    dict(allowed_hosts=("https://api.openai.com",)), dict(allowed_hosts=("*.example.org",))])
def test_policy_rejects_ambiguous_paths_and_hosts(policy):
    with pytest.raises(ValueError):
        EgressPolicy(**policy)


def test_explicit_secret_opt_out_keeps_path_and_host_controls():
    with egress_scope(EgressPolicy(block_secrets=False, allowed_hosts=("model.example.org",))):
        check_texts(("sk-" + "a" * 28,))
        with pytest.raises(SafeApplicationError):
            check_paths((".env",))


@pytest.mark.parametrize("provider", [ModelProvider.OPENAI, ModelProvider.ANTHROPIC])
def test_provider_denial_happens_before_budget_and_http(provider):
    transport = Mock(side_effect=AssertionError("不应发起 HTTP"))
    accountant = Mock()
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        reviewer = create_model_reviewer(ModelServiceSettings(provider, "fixture-model", "fixture-key"), client=client)
        with egress_scope(EgressPolicy(allowed_hosts=("not-the-provider.example",))), model_budget_scope(accountant):
            with pytest.raises(SafeApplicationError) as caught:
                reviewer.review(make_model_input())
    assert caught.value.error.code == ErrorCode.MODEL_EGRESS_DENIED
    transport.assert_not_called()
    accountant.reserve.assert_not_called()
