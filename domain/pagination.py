"""列表使用稳定排序键翻页，避免深分页扫描和把全部记录传给浏览器。"""

import base64
import binascii
import json
from datetime import UTC, datetime

from pydantic import BaseModel


class CursorPage[T](BaseModel):
    items: tuple[T, ...]
    next_cursor: str | None = None


def encode_cursor(created_at: datetime, identifier: str) -> str:
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    payload = json.dumps([created_at.isoformat(), identifier]).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_cursor(value: str) -> tuple[datetime, str]:
    try:
        if not value or len(value) > 512:
            raise ValueError
        payload = json.loads(base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True,
        ))
        if not isinstance(payload, list) or len(payload) != 2:
            raise ValueError
        date, identifier = payload
        if not isinstance(identifier, str) or not 1 <= len(identifier) <= 128:
            raise ValueError
        created_at = datetime.fromisoformat(date)
        if created_at.tzinfo is None:
            raise ValueError
        return created_at.astimezone(UTC), identifier
    except (ValueError, TypeError, UnicodeError, binascii.Error) as exc:
        raise ValueError("分页游标无效") from exc
