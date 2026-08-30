import pytest

from domain.evaluation import (
    BenchmarkGatePolicy,
    EvaluationGatePolicy,
    EvaluationMetrics,
    evaluate_benchmark,
    evaluate_benchmark_gate,
    evaluate_inline_gate,
)


def _metrics(**overrides: int) -> EvaluationMetrics:
    values = {
        "sample_count": 20,
        "valid_count": 19,
        "false_positive_count": 1,
        "high_severity_sample_count": 0,
        "high_severity_false_positive_count": 0,
    }
    values.update(overrides)
    return EvaluationMetrics(**values)


def test_gate_admits_mature_precise_risk_domain() -> None:
    result = evaluate_inline_gate(_metrics())

    assert result.admitted is True
    assert result.reason == "admitted"
    assert result.precision == pytest.approx(0.95)


def test_gate_rejects_insufficient_samples_before_precision() -> None:
    result = evaluate_inline_gate(
        _metrics(sample_count=19, valid_count=19, false_positive_count=0)
    )

    assert result.admitted is False
    assert result.reason == "insufficient_samples"


def test_gate_auto_downgrades_when_recent_precision_falls() -> None:
    result = evaluate_inline_gate(
        _metrics(sample_count=20, valid_count=17, false_positive_count=3)
    )

    assert result.admitted is False
    assert result.reason == "precision_below_threshold"


def test_gate_auto_downgrades_on_high_severity_false_positive() -> None:
    result = evaluate_inline_gate(
        _metrics(
            sample_count=40,
            valid_count=38,
            false_positive_count=2,
            high_severity_sample_count=10,
            high_severity_false_positive_count=1,
        )
    )

    assert result.admitted is False
    assert result.reason == "high_severity_false_positive_rate_above_threshold"
    assert result.high_severity_false_positive_rate == pytest.approx(0.1)


def test_all_non_valid_verdicts_reduce_precision_and_high_severity_gate() -> None:
    metrics = _metrics(
        sample_count=20,
        valid_count=16,
        false_positive_count=1,
        duplicate_count=1,
        out_of_scope_count=1,
        known_issue_count=1,
        high_severity_sample_count=4,
        high_severity_false_positive_count=0,
        high_severity_duplicate_count=1,
        high_severity_out_of_scope_count=1,
    )

    result = evaluate_inline_gate(metrics)

    assert metrics.rejected_count == 4
    assert metrics.high_severity_rejected_count == 2
    assert result.precision == pytest.approx(0.8)
    assert result.admitted is False


def test_evaluation_metrics_reject_inconsistent_counts() -> None:
    with pytest.raises(ValueError, match="verdict counts"):
        _metrics(sample_count=20, valid_count=20, false_positive_count=1)

    with pytest.raises(ValueError, match="high-severity"):
        _metrics(
            high_severity_sample_count=1,
            high_severity_false_positive_count=2,
        )


def test_policy_rejects_unbounded_or_invalid_thresholds() -> None:
    with pytest.raises(ValueError, match="sample limits"):
        EvaluationGatePolicy(minimum_samples=101, recent_sample_limit=100)
    with pytest.raises(ValueError, match="minimum precision"):
        EvaluationGatePolicy(minimum_precision=1.1)


def test_benchmark_metrics_and_regression_gate_cover_misses() -> None:
    metrics = evaluate_benchmark(
        {
            "risky": frozenset(("security:a", "database:b")),
            "clean": frozenset(),
        },
        {
            "risky": frozenset(("security:a",)),
            "clean": frozenset(("security:noise",)),
        },
    )

    assert metrics.true_positive_count == 1
    assert metrics.false_positive_count == 1
    assert metrics.false_negative_count == 1
    assert metrics.precision == pytest.approx(0.5)
    assert metrics.recall == pytest.approx(0.5)
    assert metrics.f1 == pytest.approx(0.5)
    gate = evaluate_benchmark_gate(
        metrics,
        BenchmarkGatePolicy(
            minimum_precision=0.4,
            minimum_recall=0.4,
            minimum_f1=0.4,
            baseline_precision=0.8,
            baseline_recall=0.8,
            baseline_f1=0.8,
            maximum_regression=0.05,
        ),
    )
    assert gate.admitted is False
    assert gate.failures == (
        "precision_regressed",
        "recall_regressed",
        "f1_regressed",
    )
