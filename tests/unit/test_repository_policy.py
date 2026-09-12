"""分支匹配、规则范围和真实请求前的额度边界。"""

import httpx
import pytest

from domain.enums import ModelApiProtocol, ModelProvider
from domain.repository_policy import RepositoryPolicy, RepositoryRequestLimitError
from domain.security import ErrorCode, SafeApplicationError
from services.model_budget import model_request_scope
from services.model_providers import create_model_reviewer
from services.model_review import ModelServiceSettings
from tests.unit.test_model_review import make_model_input, make_output


def test_branch_patterns_and_knowledge_selection_have_explicit_defaults():
    assert RepositoryPolicy().allows_branch(None)
    policy = RepositoryPolicy(target_branches=("main", "release/*", "main"))
    assert policy.target_branches == ("main", "release/*")
    assert policy.allows_branch("main") and policy.allows_branch("release/2026.09")
    assert not policy.allows_branch("feature/new") and not policy.allows_branch(None)
    assert RepositoryPolicy().knowledge_sources is None
    assert RepositoryPolicy(knowledge_sources=()).knowledge_sources == ()
    with pytest.raises(ValueError):
        RepositoryPolicy(target_branches=("",))
    with pytest.raises(ValueError):
        RepositoryPolicy(knowledge_sources=("../secret.md",))
    with pytest.raises(ValueError):
        RepositoryPolicy(max_model_requests=0)


def test_request_guard_blocks_format_repair_and_does_not_leak_to_next_call():
    requests = []
    invalid = [True]
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={
            "id": "test-chat",
            "choices": [{"finish_reason": "stop", "message": {
                "content": '{"invalid":true}' if invalid[0] else make_output().model_dump_json(),
            }}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 20},
        })
    settings = ModelServiceSettings(
        provider=ModelProvider.OPENAI, model="test-model", api_key="test-only-key",
        api_protocol=ModelApiProtocol.CHAT_COMPLETIONS,
        api_base_url="https://api.openai.test",
    )
    consumed = [0]
    def guard():
        if consumed[0] >= 1:
            raise RepositoryRequestLimitError()
        consumed[0] += 1
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        reviewer = create_model_reviewer(settings, client=client)
        with model_request_scope(guard), pytest.raises(SafeApplicationError) as failure:
            reviewer.review(make_model_input())
        assert failure.value.error.code is ErrorCode.MODEL_BUDGET_EXCEEDED
        assert consumed == [1] and len(requests) == 1
        invalid[0] = False
        result = reviewer.review(make_model_input())
        assert result.output.findings and len(requests) == 2
