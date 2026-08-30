"""按有限批次回填历史 Finding 派生数据。"""

from __future__ import annotations

import argparse
import json

from persistence.database import Database
from persistence.finding_backfill import SqlAlchemyFindingBackfill


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="分批回填历史 Finding 生命周期、人工裁决和评测样本",
    )
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--max-batches", type=int, default=1)
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    if not 1 <= args.batch_size <= 500:
        raise SystemExit("--batch-size 必须在 1 到 500 之间")
    if not 1 <= args.max_batches <= 1000:
        raise SystemExit("--max-batches 必须在 1 到 1000 之间")

    database = Database.from_environment()
    totals = {
        "batches": 0,
        "lifecycle_groups": 0,
        "lifecycle_findings": 0,
        "adjudications": 0,
        "evaluations": 0,
        "complete": False,
    }
    try:
        backfill = SqlAlchemyFindingBackfill(database.sessions)
        for _ in range(args.max_batches):
            batch = backfill.run_batch(args.batch_size)
            totals["batches"] += 1
            totals["lifecycle_groups"] += batch.lifecycle_groups
            totals["lifecycle_findings"] += batch.lifecycle_findings
            totals["adjudications"] += batch.adjudications
            totals["evaluations"] += batch.evaluations
            if not batch.changed:
                totals["complete"] = True
                break
    finally:
        database.dispose()
    print(json.dumps(totals, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
