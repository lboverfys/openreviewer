"""外部静态佐证和有界审查复用记录，跟随来源任务保留期清理。"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from persistence.models.base import Base


class StaticReportRecord(Base):
    __tablename__ = "static_analysis_reports"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    review_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("review_runs.id", ondelete="CASCADE"), unique=True, nullable=False)
    tool: Mapped[str] = mapped_column(String(50), nullable=False)
    tool_version: Mapped[str] = mapped_column(String(100), nullable=False)
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    base_sha: Mapped[str | None] = mapped_column(String(64))
    report_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    finding_count: Mapped[int] = mapped_column(Integer, nullable=False)
    new_count: Mapped[int] = mapped_column(Integer, nullable=False)
    existing_count: Mapped[int] = mapped_column(Integer, nullable=False)
    unknown_count: Mapped[int] = mapped_column(Integer, nullable=False)
    imported_by: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class StaticFindingRecord(Base):
    __tablename__ = "static_analysis_findings"
    __table_args__ = (Index("ix_static_findings_report_page", "report_id", "created_at", "id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    report_id: Mapped[str] = mapped_column(String(36), ForeignKey("static_analysis_reports.id", ondelete="CASCADE"), nullable=False)
    rule_id: Mapped[str] = mapped_column(String(300), nullable=False)
    file: Mapped[str] = mapped_column(String(1024), nullable=False)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)
    level: Mapped[str] = mapped_column(String(20), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    baseline_state: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ReviewReuseRecord(Base):
    __tablename__ = "review_reuse_entries"
    __table_args__ = (Index("ix_review_reuse_source", "source_run_id"),)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("review_runs.id", ondelete="CASCADE"), nullable=False)
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    result: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
