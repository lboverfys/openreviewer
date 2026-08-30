"""按仓库和风险域计算行内评论的历史评测准入。"""

from __future__ import annotations

from collections.abc import Mapping, Set
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EvaluationGatePolicy:
    """行内发布使用的保守、确定性评测门槛。"""

    minimum_samples: int = 20
    minimum_precision: float = 0.90
    maximum_high_severity_false_positive_rate: float = 0.05
    recent_sample_limit: int = 100

    def __post_init__(self) -> None:
        if not 1 <= self.minimum_samples <= self.recent_sample_limit <= 1000:
            raise ValueError("evaluation sample limits are invalid")
        if not 0 <= self.minimum_precision <= 1:
            raise ValueError("minimum precision must be between 0 and 1")
        if not 0 <= self.maximum_high_severity_false_positive_rate <= 1:
            raise ValueError(
                "maximum high-severity false-positive rate must be between 0 and 1"
            )


@dataclass(frozen=True, slots=True)
class EvaluationMetrics:
    """一个仓库风险域最近一段已裁决样本的统计值。"""

    sample_count: int
    valid_count: int
    false_positive_count: int
    high_severity_sample_count: int
    high_severity_false_positive_count: int
    duplicate_count: int = 0
    out_of_scope_count: int = 0
    known_issue_count: int = 0
    high_severity_duplicate_count: int = 0
    high_severity_out_of_scope_count: int = 0
    high_severity_known_issue_count: int = 0

    def __post_init__(self) -> None:
        values = (
            self.sample_count,
            self.valid_count,
            self.false_positive_count,
            self.high_severity_sample_count,
            self.high_severity_false_positive_count,
            self.duplicate_count,
            self.out_of_scope_count,
            self.known_issue_count,
            self.high_severity_duplicate_count,
            self.high_severity_out_of_scope_count,
            self.high_severity_known_issue_count,
        )
        if any(value < 0 for value in values):
            raise ValueError("evaluation counts must not be negative")
        if self.valid_count + self.rejected_count != self.sample_count:
            raise ValueError("evaluation verdict counts must equal sample count")
        if self.high_severity_sample_count > self.sample_count:
            raise ValueError("high-severity samples cannot exceed all samples")
        if self.high_severity_rejected_count > self.high_severity_sample_count:
            raise ValueError(
                "high-severity rejections cannot exceed high-severity samples"
            )

    @property
    def rejected_count(self) -> int:
        return (
            self.false_positive_count
            + self.duplicate_count
            + self.out_of_scope_count
            + self.known_issue_count
        )

    @property
    def high_severity_rejected_count(self) -> int:
        return (
            self.high_severity_false_positive_count
            + self.high_severity_duplicate_count
            + self.high_severity_out_of_scope_count
            + self.high_severity_known_issue_count
        )

    @property
    def precision(self) -> float:
        return self.valid_count / self.sample_count if self.sample_count else 0.0

    @property
    def high_severity_false_positive_rate(self) -> float:
        """兼容旧字段：返回所有高风险人工否决占比。"""

        if not self.high_severity_sample_count:
            return 0.0
        return (
            self.high_severity_rejected_count
            / self.high_severity_sample_count
        )


@dataclass(frozen=True, slots=True)
class EvaluationGateResult:
    """供详情页和发布器共同消费的准入快照。"""

    admitted: bool
    reason: str
    precision: float
    high_severity_false_positive_rate: float


def evaluate_inline_gate(
    metrics: EvaluationMetrics,
    policy: EvaluationGatePolicy | None = None,
) -> EvaluationGateResult:
    """根据最近真实人工裁决决定一个风险域是否允许行内发布。"""

    selected = policy or EvaluationGatePolicy()
    if metrics.sample_count < selected.minimum_samples:
        return EvaluationGateResult(
            admitted=False,
            reason="insufficient_samples",
            precision=metrics.precision,
            high_severity_false_positive_rate=(
                metrics.high_severity_false_positive_rate
            ),
        )
    if metrics.precision < selected.minimum_precision:
        return EvaluationGateResult(
            admitted=False,
            reason="precision_below_threshold",
            precision=metrics.precision,
            high_severity_false_positive_rate=(
                metrics.high_severity_false_positive_rate
            ),
        )
    if (
        metrics.high_severity_false_positive_rate
        > selected.maximum_high_severity_false_positive_rate
    ):
        return EvaluationGateResult(
            admitted=False,
            reason="high_severity_false_positive_rate_above_threshold",
            precision=metrics.precision,
            high_severity_false_positive_rate=(
                metrics.high_severity_false_positive_rate
            ),
        )
    return EvaluationGateResult(
        admitted=True,
        reason="admitted",
        precision=metrics.precision,
        high_severity_false_positive_rate=(
            metrics.high_severity_false_positive_rate
        ),
    )


@dataclass(frozen=True, slots=True)
class BenchmarkMetrics:
    """黄金集逐案例精确匹配后的二分类指标。"""

    true_positive_count: int
    false_positive_count: int
    false_negative_count: int

    def __post_init__(self) -> None:
        if any(
            value < 0
            for value in (
                self.true_positive_count,
                self.false_positive_count,
                self.false_negative_count,
            )
        ):
            raise ValueError("benchmark counts must not be negative")

    @property
    def precision(self) -> float:
        predicted = self.true_positive_count + self.false_positive_count
        if predicted:
            return self.true_positive_count / predicted
        return 1.0 if self.false_negative_count == 0 else 0.0

    @property
    def recall(self) -> float:
        expected = self.true_positive_count + self.false_negative_count
        return self.true_positive_count / expected if expected else 1.0

    @property
    def f1(self) -> float:
        total = self.precision + self.recall
        return 2 * self.precision * self.recall / total if total else 0.0


@dataclass(frozen=True, slots=True)
class BenchmarkGatePolicy:
    """绝对质量下限与相对上一基线的最大允许退化。"""

    minimum_precision: float
    minimum_recall: float
    minimum_f1: float
    baseline_precision: float
    baseline_recall: float
    baseline_f1: float
    maximum_regression: float = 0.02

    def __post_init__(self) -> None:
        values = (
            self.minimum_precision,
            self.minimum_recall,
            self.minimum_f1,
            self.baseline_precision,
            self.baseline_recall,
            self.baseline_f1,
            self.maximum_regression,
        )
        if any(not 0 <= value <= 1 for value in values):
            raise ValueError("benchmark thresholds must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class BenchmarkGateResult:
    admitted: bool
    failures: tuple[str, ...]


def evaluate_benchmark(
    expected_by_case: Mapping[str, Set[str]],
    predicted_by_case: Mapping[str, Set[str]],
) -> BenchmarkMetrics:
    """按案例隔离 Finding 键，避免不同案例中的同名键相互抵消。"""

    expected_cases = set(expected_by_case)
    predicted_cases = set(predicted_by_case)
    if expected_cases != predicted_cases:
        raise ValueError("golden and prediction case ids must match")
    expected = {
        (case_id, finding_key)
        for case_id, finding_keys in expected_by_case.items()
        for finding_key in finding_keys
    }
    predicted = {
        (case_id, finding_key)
        for case_id, finding_keys in predicted_by_case.items()
        for finding_key in finding_keys
    }
    return BenchmarkMetrics(
        true_positive_count=len(expected & predicted),
        false_positive_count=len(predicted - expected),
        false_negative_count=len(expected - predicted),
    )


def evaluate_benchmark_gate(
    metrics: BenchmarkMetrics,
    policy: BenchmarkGatePolicy,
) -> BenchmarkGateResult:
    """同时执行绝对门槛与相对基线回归检查。"""

    failures: list[str] = []
    values = {
        "precision": metrics.precision,
        "recall": metrics.recall,
        "f1": metrics.f1,
    }
    minimums = {
        "precision": policy.minimum_precision,
        "recall": policy.minimum_recall,
        "f1": policy.minimum_f1,
    }
    baselines = {
        "precision": policy.baseline_precision,
        "recall": policy.baseline_recall,
        "f1": policy.baseline_f1,
    }
    for name in ("precision", "recall", "f1"):
        if values[name] < minimums[name]:
            failures.append(f"{name}_below_minimum")
        if values[name] < baselines[name] - policy.maximum_regression:
            failures.append(f"{name}_regressed")
    return BenchmarkGateResult(
        admitted=not failures,
        failures=tuple(failures),
    )
