import pytest
from pydantic import ValidationError

from domain.enums import (
    CoverageStatus,
    ExecutionStatus,
    FindingCategory,
    LocationSide,
    PullRequestAction,
    ReviewConclusion,
    Severity,
    VerificationStatus,
)
from domain.identifiers import build_review_version_key, build_thread_id
from domain.models import (
    FindingLocation,
    PullRequestWebhook,
    ReviewFinding,
    ReviewRunState,
    ReviewVersion,
)


HEAD_SHA = "A" * 40
BASE_SHA = "B" * 40


def make_location(**overrides: object) -> FindingLocation:
    values: dict[str, object] = {
        "file": "src/example.py",
        "start_line": 12,
        "end_line": 12,
        "in_diff": True,
        "side": LocationSide.RIGHT,
    }
    values.update(overrides)
    return FindingLocation.model_validate(values)


def make_finding(**overrides: object) -> ReviewFinding:
    values: dict[str, object] = {
        "fingerprint": "auth-resource-owner",
        "head_sha": HEAD_SHA,
        "severity": Severity.HIGH,
        "category": FindingCategory.AUTHORIZATION,
        "location": make_location(),
        "title": "资源归属未校验",
        "evidence": "服务直接使用请求中的 user_id。",
        "impact": "已登录用户可能读取其他用户资源。",
        "suggestion": "从服务端登录态读取用户并校验资源归属。",
        "required_test": "增加跨用户访问被拒绝的测试。",
        "confidence": 0.93,
        "verification_status": VerificationStatus.VERIFIED,
        "rule_reference": "AUTH-001",
    }
    values.update(overrides)
    return ReviewFinding.model_validate(values)


def test_review_version_key_normalizes_sha() -> None:
    key = build_review_version_key(42, 128, HEAD_SHA)

    assert key == f"42:128:{'a' * 40}"


def test_thread_id_adds_run_identity() -> None:
    thread_id = build_thread_id(42, 128, HEAD_SHA, "run-001")

    assert thread_id == f"42:128:{'a' * 40}:run-001"


def test_webhook_accepts_only_supported_pull_request_action() -> None:
    event = PullRequestWebhook(
        action=PullRequestAction.SYNCHRONIZE,
        delivery_id="delivery-001",
        installation_id=10,
        repository_id=42,
        repository="example/repo",
        pull_request_number=128,
        head_sha=HEAD_SHA,
    )

    assert event.head_sha == "a" * 40
    assert event.review_version_key == f"42:128:{'a' * 40}"
    assert event.deduplication_key == "delivery-001"

    with pytest.raises(ValidationError):
        PullRequestWebhook(
            action="closed",
            delivery_id="delivery-002",
            installation_id=10,
            repository_id=42,
            repository="example/repo",
            pull_request_number=128,
            head_sha=HEAD_SHA,
        )


def test_finding_serializes_stable_lowercase_values() -> None:
    finding = make_finding()
    payload = finding.model_dump(mode="json")

    assert payload["severity"] == "high"
    assert payload["category"] == "authorization"
    assert payload["verification_status"] == "verified"
    assert payload["location"]["side"] == "right"
    assert finding.can_publish_inline(HEAD_SHA) is True


def test_unverified_or_non_diff_finding_cannot_publish_inline() -> None:
    assert (
        make_finding(
            verification_status=VerificationStatus.UNVERIFIED
        ).can_publish_inline(HEAD_SHA)
        is False
    )
    assert (
        make_finding(location=make_location(in_diff=False)).can_publish_inline(
            HEAD_SHA
        )
        is False
    )
    assert (
        make_finding(
            location=make_location(side=LocationSide.LEFT)
        ).can_publish_inline(HEAD_SHA)
        is False
    )
    assert make_finding(location=None).can_publish_inline(HEAD_SHA) is False


def test_stale_low_confidence_or_test_gap_finding_cannot_publish_inline() -> None:
    assert make_finding().can_publish_inline("c" * 40) is False
    assert make_finding(confidence=0.89).can_publish_inline(HEAD_SHA) is False
    assert (
        make_finding(category=FindingCategory.TEST_GAP).can_publish_inline(HEAD_SHA)
        is False
    )


def test_location_rejects_invalid_range_and_traversal() -> None:
    with pytest.raises(ValidationError):
        make_location(start_line=20, end_line=19)

    with pytest.raises(ValidationError):
        make_location(file="../secrets.txt")


def test_finding_rejects_invalid_confidence_and_sha() -> None:
    with pytest.raises(ValidationError):
        make_finding(confidence=1.01)

    with pytest.raises(ValidationError):
        make_finding(head_sha="short")


def test_completed_run_requires_a_conclusion() -> None:
    version = ReviewVersion(
        repository_id=42,
        repository="example/repo",
        pull_request_number=128,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
    )

    with pytest.raises(ValidationError):
        ReviewRunState(
            review_run_id="run-001",
            version=version,
            execution_status=ExecutionStatus.COMPLETED,
            coverage_status=CoverageStatus.COMPLETE,
        )

    state = ReviewRunState(
        review_run_id="run-001",
        version=version,
        execution_status=ExecutionStatus.COMPLETED,
        review_conclusion=ReviewConclusion.FINDINGS_PRESENT,
        coverage_status=CoverageStatus.COMPLETE,
    )
    assert state.version.review_version_key == f"42:128:{'a' * 40}"
    assert state.version.is_current(HEAD_SHA) is True
    assert state.version.is_current("c" * 40) is False


def test_platform_failure_is_not_a_clean_review_result() -> None:
    version = ReviewVersion(
        repository_id=42,
        repository="example/repo",
        pull_request_number=128,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
    )

    failed = ReviewRunState(
        review_run_id="run-failed",
        version=version,
        execution_status=ExecutionStatus.FAILED,
        review_conclusion=ReviewConclusion.INDETERMINATE,
        coverage_status=CoverageStatus.UNKNOWN,
    )
    clean = ReviewRunState(
        review_run_id="run-clean",
        version=version,
        execution_status=ExecutionStatus.COMPLETED,
        review_conclusion=ReviewConclusion.NO_CONFIRMED_FINDINGS,
        coverage_status=CoverageStatus.COMPLETE,
    )

    assert failed.review_conclusion is ReviewConclusion.INDETERMINATE
    assert clean.review_conclusion is ReviewConclusion.NO_CONFIRMED_FINDINGS
