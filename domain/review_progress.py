"""从持久化批次读取的进度和按需展示的批次摘要。"""

from pydantic import BaseModel


class BatchProgress(BaseModel):
    total: int
    completed: int
    failed: int
    running: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    duration_ms: int | None = None


class BatchSnapshot(BaseModel):
    batch_number: int
    status: str
    duration_ms: int | None
    error_code: str | None
    error_message: str | None
