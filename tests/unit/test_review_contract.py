import pytest
from pydantic import ValidationError

from domain.enums import (
    CoverageStatus,
    ExecutionStatus,
    FindingAdjudicationStatus,
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
    """构造默认满足行内评论定位条件的 ``FindingLocation``。

    参数：
        overrides: 覆盖默认文件、行号、diff 标志或左右侧的字段，便于单个测试只
            改变一个门槛。

    返回：
        经过 Pydantic 完整校验的定位对象。默认是 ``src/example.py`` 第 12 行、
        当前 diff 的右侧。
    """
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
    """构造默认满足全部行内评论门槛的高置信度 Finding。

    参数：
        overrides: 要替换的 Finding 字段，用于隔离测试某一个拒绝条件。

    返回：
        已验证、高严重度、置信度 0.93、绑定当前提交并带右侧 diff 定位的 Finding。

    所有测试都从同一合法基线出发，失败断言因此能明确归因于被覆盖字段。
    """
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
        "adjudication_status": FindingAdjudicationStatus.VALID,
        "rule_reference": "AUTH-001",
    }
    values.update(overrides)
    return ReviewFinding.model_validate(values)


def test_review_version_key_normalizes_sha() -> None:
    """验证版本键会规范化 SHA，且相同输入得到稳定结果。

    动作：使用大写 40 位 SHA 构造版本键。
    预期：仓库 ID 和 PR 编号顺序不变，SHA 被转成小写，得到可用于数据库索引和
    跨请求比较的确定字符串。
    """
    key = build_review_version_key(42, 128, HEAD_SHA)

    assert key == f"42:128:{'a' * 40}"


def test_thread_id_adds_run_identity() -> None:
    """验证线程 ID 在版本键后追加一次运行身份。

    动作：为固定仓库、PR、提交和 ``run-001`` 构造线程 ID。
    预期：结果保留规范化版本键并追加运行 ID，使同一提交的显式重新审查可以拥有
    不同评论线程，而不会互相覆盖。
    """
    thread_id = build_thread_id(42, 128, HEAD_SHA, "run-001")

    assert thread_id == f"42:128:{'a' * 40}:run-001"


def test_webhook_accepts_only_supported_pull_request_action() -> None:
    """验证 Webhook 模型只接受契约允许的 PR 动作。

    前提：构造合法的 ``synchronize`` 事件。
    预期：SHA 规范化、版本键正确、Delivery ID 原样作为去重键；随后把动作改成
    不支持的 ``closed``，Pydantic 必须抛出 ``ValidationError``，阻止其创建任务。
    """
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
    """验证 Finding 对外序列化和行内候选判断使用稳定契约值。

    动作：序列化默认合法 Finding，并用当前 head SHA 检查行内资格。
    预期：严重度、分类、复核状态和位置侧均为小写 JSON 字符串，同时所有门槛
    满足时 ``can_publish_inline`` 返回 True。
    """
    finding = make_finding()
    payload = finding.model_dump(mode="json")

    assert payload["severity"] == "high"
    assert payload["category"] == "authorization"
    assert payload["verification_status"] == "verified"
    assert payload["adjudication_status"] == "valid"
    assert payload["location"]["side"] == "right"
    assert finding.can_publish_inline(HEAD_SHA) is True


def test_unverified_or_non_diff_finding_cannot_publish_inline() -> None:
    """验证定位和人工复核相关门槛会阻止行内发布。

    动作：分别把合法 Finding 改成未复核、不在 diff、位于旧代码左侧或完全无定位。
    预期：四种情况都返回 False；它们仍可进入摘要，但不能伪造 GitHub 行内位置。

    每个断言只改变一个条件，便于失败时准确判断是哪一道发布门槛失效。
    """
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
    assert (
        make_finding(
            adjudication_status=FindingAdjudicationStatus.FALSE_POSITIVE
        ).can_publish_inline(HEAD_SHA)
        is False
    )


def test_stale_low_confidence_or_test_gap_finding_cannot_publish_inline() -> None:
    """验证提交新鲜度、置信度和问题分类门槛。

    动作：分别传入不同的当前 SHA、低于默认 0.90 的置信度和 ``test_gap`` 分类。
    预期：三种 Finding 都不能成为行内候选，避免旧结果覆盖新提交、低把握结果
    造成噪声，或把普通测试建议散落成行内评论。
    """
    assert make_finding().can_publish_inline("c" * 40) is False
    assert make_finding(confidence=0.89).can_publish_inline(HEAD_SHA) is False
    assert (
        make_finding(category=FindingCategory.TEST_GAP).can_publish_inline(HEAD_SHA)
        is False
    )


def test_location_rejects_invalid_range_and_traversal() -> None:
    """验证 Finding 定位拒绝倒置行号和父目录穿越。

    动作：先让结束行早于开始行，再把文件路径设置为 ``../secrets.txt``。
    预期：两次模型构造都抛出 ``ValidationError``，防止生成无效 GitHub 定位或让
    后续文件读取逃出仓库根目录。
    """
    with pytest.raises(ValidationError):
        make_location(start_line=20, end_line=19)

    with pytest.raises(ValidationError):
        make_location(file="../secrets.txt")


def test_finding_rejects_invalid_confidence_and_sha() -> None:
    """验证 Finding 拒绝越界置信度和不完整 SHA。

    动作：分别提供 1.01 置信度和 ``short`` SHA。
    预期：Pydantic 均拒绝构造，确保策略层收到的数据可以安全执行数值比较和提交
    新鲜度判断。
    """
    with pytest.raises(ValidationError):
        make_finding(confidence=1.01)

    with pytest.raises(ValidationError):
        make_finding(head_sha="short")


def test_completed_run_requires_a_conclusion() -> None:
    """验证 ``completed`` 状态必须同时带明确审查结论。

    前提：构造一个合法审查版本。
    动作：先创建无结论的 completed 运行，再创建带 findings 结论的 completed 运行。
    预期：前者校验失败，后者成功；同时验证版本键和 ``is_current`` 能区分当前/旧 SHA。
    """
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
    """验证平台失败与“审查完成且未发现问题”保持不同语义。

    动作：构造失败且结论不确定的运行，以及完成且无确认问题的运行。
    预期：两个结论分别保留 ``indeterminate`` 和 ``no_confirmed_findings``，证明
    模型/平台故障不会被序列化成干净通过结果。
    """
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
