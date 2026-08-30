"""校验真实模型评测数据并输出不包含原始模型文本的聚合报告。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pydantic import ValidationError

from domain.real_evaluation import RealEvaluationDataset, summarize_real_evaluation

_MAX_INPUT_BYTES = 32 * 1024 * 1024


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="校验真实模型观测和双人裁决，并输出聚合质量报告",
    )
    parser.add_argument("input", type=Path)
    return parser.parse_args()


def load_dataset(path: Path) -> RealEvaluationDataset:
    try:
        if path.stat().st_size > _MAX_INPUT_BYTES:
            raise ValueError("真实评测文件超过 32 MiB 上限")
        payload = json.loads(path.read_text(encoding="utf-8"))
        return RealEvaluationDataset.model_validate(payload)
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(f"真实评测文件无效：{path}") from exc


def main() -> int:
    arguments = _arguments()
    try:
        dataset = load_dataset(arguments.input)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(
        json.dumps(
            summarize_real_evaluation(dataset),
            ensure_ascii=True,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
