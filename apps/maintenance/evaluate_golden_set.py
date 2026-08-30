"""回放录制输出夹具，并对生产审查边界执行 Finding 契约门禁。"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from domain.evaluation import (
    BenchmarkGatePolicy,
    evaluate_benchmark,
    evaluate_benchmark_gate,
)
from domain.model_review import PROMPT_VERSION
from services.evaluation_flow import GoldenFlowCase, run_golden_review_flow

_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_CASES = 1000
_MAX_FINDINGS_PER_CASE = 500


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="计算黄金集 precision、recall、F1 并执行基线回归门禁",
    )
    parser.add_argument(
        "--golden",
        type=Path,
        default=Path("tests/evaluation/golden_set.json"),
    )
    parser.add_argument(
        "--predictions",
        type=Path,
        default=Path("tests/evaluation/current_predictions.json"),
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=Path("tests/evaluation/baseline.json"),
    )
    return parser.parse_args()


def _read_object(path: Path) -> Mapping[str, Any]:
    if path.stat().st_size > _MAX_FILE_BYTES:
        raise ValueError(f"evaluation file is too large: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"evaluation file must contain an object: {path}")
    return value


def _case_map(path: Path, finding_field: str) -> dict[str, frozenset[str]]:
    payload = _read_object(path)
    if payload.get("schema_version") != 1:
        raise ValueError(f"unsupported evaluation schema: {path}")
    cases = payload.get("cases")
    if not isinstance(cases, list) or not 1 <= len(cases) <= _MAX_CASES:
        raise ValueError(f"evaluation cases are invalid: {path}")
    result: dict[str, frozenset[str]] = {}
    for item in cases:
        if not isinstance(item, dict):
            raise ValueError(f"evaluation case must be an object: {path}")
        case_id = item.get("case_id")
        finding_keys = item.get(finding_field)
        if (
            not isinstance(case_id, str)
            or not case_id
            or len(case_id) > 200
            or case_id in result
        ):
            raise ValueError(f"evaluation case id is invalid: {path}")
        if (
            not isinstance(finding_keys, list)
            or len(finding_keys) > _MAX_FINDINGS_PER_CASE
            or any(
                not isinstance(key, str) or not key or len(key) > 512
                for key in finding_keys
            )
            or len(set(finding_keys)) != len(finding_keys)
        ):
            raise ValueError(f"evaluation finding keys are invalid: {case_id}")
        result[case_id] = frozenset(finding_keys)
    return result


def _flow_cases(
    golden_path: Path,
    prediction_path: Path,
) -> tuple[GoldenFlowCase, ...]:
    golden = _read_object(golden_path)
    predictions = _read_object(prediction_path)
    provenance = predictions.get("provenance")
    if (
        not isinstance(provenance, dict)
        or provenance.get("source") != "recorded-output-fixture"
        or provenance.get("prompt_version") != PROMPT_VERSION
        or provenance.get("provider") != "openai"
        or provenance.get("api_protocol") != "responses"
    ):
        raise ValueError("prediction snapshot provenance is missing or stale")
    predicted = _case_map(prediction_path, "predicted_finding_keys")
    raw_cases = golden.get("cases")
    if not isinstance(raw_cases, list):
        raise ValueError("golden evaluation cases are invalid")
    result: list[GoldenFlowCase] = []
    for item in raw_cases:
        if not isinstance(item, dict):
            raise ValueError("golden evaluation case must be an object")
        case_id = item.get("case_id")
        file = item.get("file")
        patch = item.get("patch")
        if (
            not isinstance(case_id, str)
            or not isinstance(file, str)
            or not file
            or not isinstance(patch, str)
            or not patch
            or case_id not in predicted
        ):
            raise ValueError("golden flow case is incomplete")
        result.append(
            GoldenFlowCase(
                case_id=case_id,
                file=file,
                patch=patch,
                predicted_finding_keys=predicted[case_id],
            )
        )
    if set(predicted) != {item.case_id for item in result}:
        raise ValueError("golden and prediction case ids must match")
    return tuple(result)


def _number(payload: Mapping[str, Any], name: str) -> float:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"benchmark baseline field is invalid: {name}")
    return float(value)


def main() -> int:
    args = _arguments()
    expected = _case_map(args.golden, "expected_finding_keys")
    predicted = run_golden_review_flow(_flow_cases(args.golden, args.predictions))
    baseline = _read_object(args.baseline)
    if baseline.get("schema_version") != 1:
        raise ValueError("unsupported benchmark baseline schema")
    policy = BenchmarkGatePolicy(
        minimum_precision=_number(baseline, "minimum_precision"),
        minimum_recall=_number(baseline, "minimum_recall"),
        minimum_f1=_number(baseline, "minimum_f1"),
        baseline_precision=_number(baseline, "baseline_precision"),
        baseline_recall=_number(baseline, "baseline_recall"),
        baseline_f1=_number(baseline, "baseline_f1"),
        maximum_regression=_number(baseline, "maximum_regression"),
    )
    metrics = evaluate_benchmark(expected, predicted)
    result = evaluate_benchmark_gate(metrics, policy)
    print(
        json.dumps(
            {
                "admitted": result.admitted,
                "case_count": len(expected),
                "execution_mode": "recorded_output_replay",
                "failures": result.failures,
                "metric_scope": "pipeline_contract_regression",
                "real_model_accuracy": None,
                "true_positive_count": metrics.true_positive_count,
                "false_positive_count": metrics.false_positive_count,
                "false_negative_count": metrics.false_negative_count,
                "precision": metrics.precision,
                "recall": metrics.recall,
                "f1": metrics.f1,
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )
    return 0 if result.admitted else 1


if __name__ == "__main__":
    raise SystemExit(main())
