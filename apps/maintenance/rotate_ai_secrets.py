"""按有限批次轮换数据库中的 AI API Key 加密版本。"""

from __future__ import annotations

import argparse
import json

from persistence.database import Database
from services.ai_secret_rotation import AiSecretRotationService
from services.ai_settings import AiSecretCipher


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把旧密钥版本加密的 AI API Key 分批重加密为当前版本",
    )
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--max-batches", type=int, default=1)
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    if not 1 <= args.batch_size <= 500:
        raise SystemExit("--batch-size 必须在 1 到 500 之间")
    if not 1 <= args.max_batches <= 1000:
        raise SystemExit("--max-batches 必须在 1 到 1000 之间")

    database = Database.from_environment()
    cipher = AiSecretCipher.from_environment()
    totals = {
        "key_version": cipher.key_version,
        "batches": 0,
        "provider_secrets": 0,
        "agent_secrets": 0,
        "complete": False,
    }
    try:
        rotation = AiSecretRotationService(database.sessions, cipher)
        for _ in range(args.max_batches):
            batch = rotation.rotate_batch(args.batch_size)
            totals["batches"] += 1
            totals["provider_secrets"] += batch.provider_secrets
            totals["agent_secrets"] += batch.agent_secrets
            if batch.complete:
                totals["complete"] = True
                break
    finally:
        database.dispose()
    print(json.dumps(totals, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
