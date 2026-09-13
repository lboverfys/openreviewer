"""Worker 在线摘要与独立分页契约，心跳记录不等同于永久执行节点。"""

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from domain.enums import WorkerStatus
from domain.pagination import CursorPage

WORKER_ONLINE_WINDOW = timedelta(seconds=15)
DASHBOARD_WORKER_PREVIEW_LIMIT = 3
WorkerListState = Literal["online", "offline", "all"]


class WorkerNode(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    worker_id: str
    status: WorkerStatus
    online: bool
    current_task_id: str | None
    current_review_run_id: str | None
    started_at: datetime
    last_seen_at: datetime


class WorkerNodePage(CursorPage[WorkerNode]):
    generated_at: datetime
    retention_days: int = Field(ge=1)
    online_window_seconds: int = 15
