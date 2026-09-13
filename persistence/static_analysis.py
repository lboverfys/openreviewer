"""静态报告按任务授权；不可变导入，线索游标分页和同位置 AI 计数。"""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, insert, select

from domain.pagination import CursorPage, encode_cursor
from domain.platform import PlatformConflictError, PlatformNotFoundError
from domain.static_analysis import (
    StaticFindingView,
    StaticReportUpload,
    StaticReportView,
)
from persistence.models import (
    PullRequestVersionRecord,
    ReviewFindingRecord,
    ReviewRunRecord,
    StaticFindingRecord,
    StaticReportRecord,
)
from persistence.pagination import apply_cursor
from persistence.platform_common import platform_audit
from persistence.resource_scope import resource_predicate
from services.static_analysis import parse_upload

_REPORT_COLUMNS = tuple(getattr(StaticReportRecord, key) for key in StaticReportView.model_fields)


class StaticAnalysisRepository:
    def __init__(self, sessions):
        self.sessions = sessions

    @staticmethod
    def _target(session, run_id, scope, *, lock=False):
        run = ReviewRunRecord
        query = select(run.repository, run.head_sha, PullRequestVersionRecord.base_sha).outerjoin(
            PullRequestVersionRecord, PullRequestVersionRecord.review_version_key == run.review_version_key,
        ).where(run.id == run_id, resource_predicate(scope,
            installation_column=run.installation_id, repository_column=run.repository,
            repository_key_column=run.repository_key))
        if lock:
            query = query.with_for_update(of=run, read=True)
        target = session.execute(query).one_or_none()
        if target is None:
            raise PlatformNotFoundError("审查任务不存在")
        return target

    def get(self, run_id, scope):
        with self.sessions() as session:
            self._target(session, run_id, scope)
            row = session.execute(select(*_REPORT_COLUMNS).where(
                StaticReportRecord.review_run_id == run_id)).mappings().one_or_none()
            return StaticReportView.model_validate(row) if row else None

    def upload(self, run_id: str, draft: StaticReportUpload, actor: str, scope):
        # 授权先于解析；SHA 校验和保存时再以共享锁保证任务不会被清理。
        with self.sessions() as session:
            self._target(session, run_id, scope)
        version, findings, digest = parse_upload(draft)
        now = datetime.now(UTC)
        with self.sessions() as session, session.begin():
            target = self._target(session, run_id, scope, lock=True)
            if target.head_sha != draft.head_sha or (draft.base_sha and target.base_sha != draft.base_sha):
                raise ValueError("报告提交必须匹配此任务固定的 head/base SHA")
            existing = session.execute(select(*_REPORT_COLUMNS).where(
                StaticReportRecord.review_run_id == run_id)).mappings().one_or_none()
            if existing:
                if existing["report_hash"] != digest:
                    raise PlatformConflictError("该任务已有不可变静态报告，请在新审查任务中导入新报告")
                return StaticReportView.model_validate(existing)
            view = StaticReportView(id=str(uuid4()), review_run_id=run_id, tool="Semgrep",
                tool_version=version, head_sha=draft.head_sha, base_sha=draft.base_sha,
                report_hash=digest, finding_count=len(findings),
                new_count=sum(item["baseline_state"] == "new" for item in findings),
                existing_count=sum(item["baseline_state"] == "existing" for item in findings),
                unknown_count=sum(item["baseline_state"] == "unknown" for item in findings),
                imported_by=actor, created_at=now)
            session.add(StaticReportRecord(**view.model_dump()))
            session.flush()
            if findings:
                session.execute(insert(StaticFindingRecord), [dict(id=str(uuid4()), report_id=view.id,
                    created_at=now, **item) for item in findings])
            platform_audit(session, "platform.static.imported", run_id, target.repository,
                           actor, now, details={"report_hash": digest, "finding_count": len(findings)})
            return view

    def findings(self, run_id, scope, *, limit=10, cursor=None):
        if not 1 <= limit <= 100:
            raise ValueError("分页大小应为 1 到 100")
        item, ai = StaticFindingRecord, ReviewFindingRecord
        with self.sessions() as session:
            self._target(session, run_id, scope)
            report_id = session.scalar(select(StaticReportRecord.id).where(StaticReportRecord.review_run_id == run_id))
            if report_id is None:
                return CursorPage[StaticFindingView](items=(), next_cursor=None)
            overlapping = select(func.count(ai.id)).where(
                ai.review_run_id == run_id, ai.location_file == item.file,
                ai.location_side == "right", ai.location_start_line <= item.end_line,
                ai.location_end_line >= item.start_line,
            ).correlate(item).scalar_subquery()
            columns = [getattr(item, key) for key in StaticFindingView.model_fields if key != "overlapping_ai_count"]
            statement = select(*columns, overlapping.label("overlapping_ai_count")).where(item.report_id == report_id)
            rows = session.execute(apply_cursor(statement, item.created_at, item.id, cursor).limit(limit + 1)).mappings().all()
        items = tuple(StaticFindingView.model_validate(row) for row in rows[:limit])
        return CursorPage[StaticFindingView](items=items,
            next_cursor=encode_cursor(items[-1].created_at, items[-1].id) if len(rows) > limit else None)
