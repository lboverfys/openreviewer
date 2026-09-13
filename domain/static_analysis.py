"""Semgrep SARIF 导入契约；静态线索与人工裁决保持独立。"""

from datetime import datetime
from typing import Literal

from pydantic import Field

from domain.platform import PlatformModel


class StaticReportUpload(PlatformModel):
    head_sha: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    base_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40,64}$")
    head_sarif: str = Field(min_length=2, max_length=1_000_000)
    base_sarif: str | None = Field(default=None, min_length=2, max_length=1_000_000)


class StaticReportView(PlatformModel):
    id: str
    review_run_id: str
    tool: str
    tool_version: str
    head_sha: str
    base_sha: str | None
    report_hash: str
    finding_count: int
    new_count: int
    existing_count: int
    unknown_count: int
    imported_by: str
    created_at: datetime


class StaticFindingView(PlatformModel):
    id: str
    rule_id: str
    file: str
    start_line: int
    end_line: int
    level: str
    message: str
    baseline_state: Literal["new", "existing", "unknown"]
    overlapping_ai_count: int = 0
    created_at: datetime
