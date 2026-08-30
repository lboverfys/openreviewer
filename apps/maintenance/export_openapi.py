"""生成或校验供前端使用的稳定 OpenAPI 契约快照。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from apps.api.main import app


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成或校验 OpenAPI 契约快照")
    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        default=Path("docs/openapi.json"),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只比较现有快照，不写文件",
    )
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    schema = app.openapi()
    if arguments.check:
        try:
            stored = json.loads(arguments.output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"OpenAPI 快照无法读取：{arguments.output}") from exc
        if stored != schema:
            raise SystemExit(
                "OpenAPI 快照已过期，请运行 "
                f"python -m apps.maintenance.export_openapi {arguments.output}"
            )
        print(f"OpenAPI 快照与后端契约一致：{arguments.output}")
        return 0

    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(schema, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"已生成 OpenAPI 快照：{arguments.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
