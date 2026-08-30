"""生成或校验真实模型评测数据的 JSON Schema 快照。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from domain.real_evaluation import RealEvaluationDataset


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成或校验真实评测 JSON Schema")
    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        default=Path("docs/contracts/real-evaluation.schema.json"),
    )
    parser.add_argument("--check", action="store_true")
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    schema = RealEvaluationDataset.model_json_schema()
    if arguments.check:
        try:
            stored = json.loads(arguments.output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"真实评测 Schema 无法读取：{arguments.output}") from exc
        if stored != schema:
            raise SystemExit(
                "真实评测 Schema 已过期，请运行 python -m "
                "apps.maintenance.export_real_evaluation_schema"
            )
        print(f"真实评测 Schema 与领域契约一致：{arguments.output}")
        return 0
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(schema, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"已生成真实评测 Schema：{arguments.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
