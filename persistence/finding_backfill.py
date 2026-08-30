"""Finding 历史生命周期与人工评测的有界回填。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session, sessionmaker

from domain.enums import FindingAdjudicationStatus, FindingLifecycleState
from persistence.models import (
    FindingEvaluationRecord,
    FindingLifecycleRecord,
    ReviewFindingRecord,
    ReviewRunRecord,
)


@dataclass(frozen=True, slots=True)
class FindingBackfillBatch:
    lifecycle_groups: int
    lifecycle_findings: int
    adjudications: int
    evaluations: int

    @property
    def changed(self) -> bool:
        return any(
            (
                self.lifecycle_groups,
                self.lifecycle_findings,
                self.adjudications,
                self.evaluations,
            )
        )


@dataclass(frozen=True, slots=True)
class _Occurrence:
    finding: ReviewFindingRecord
    repository_id: int
    pull_request_number: int


class SqlAlchemyFindingBackfill:
    """每次用固定次数批量查询和写入处理一小批历史 Finding。"""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def run_batch(
        self,
        batch_size: int = 100,
        *,
        now: datetime | None = None,
    ) -> FindingBackfillBatch:
        if not 1 <= batch_size <= 500:
            raise ValueError("finding backfill batch size must be between 1 and 500")
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        with self._sessions.begin() as session:
            lifecycle_groups, lifecycle_findings = self._backfill_lifecycles(
                session,
                batch_size,
                timestamp,
            )
            adjudications = self._backfill_adjudications(
                session,
                batch_size,
            )
            session.flush()
            evaluations = self._backfill_evaluations(
                session,
                batch_size,
                timestamp,
            )
            return FindingBackfillBatch(
                lifecycle_groups=lifecycle_groups,
                lifecycle_findings=lifecycle_findings,
                adjudications=adjudications,
                evaluations=evaluations,
            )

    @staticmethod
    def _backfill_lifecycles(
        session: Session,
        batch_size: int,
        now: datetime,
    ) -> tuple[int, int]:
        occurrence_rows = session.execute(
            select(
                ReviewFindingRecord,
                ReviewRunRecord.repository_id,
                ReviewRunRecord.pull_request_number,
            )
            .join(
                ReviewRunRecord,
                ReviewRunRecord.id == ReviewFindingRecord.review_run_id,
            )
            .where(ReviewFindingRecord.lifecycle_backfilled_at.is_(None))
            .order_by(
                ReviewFindingRecord.created_at,
                ReviewFindingRecord.id,
            )
            .limit(batch_size)
            .with_for_update(of=ReviewFindingRecord)
        ).all()
        if not occurrence_rows:
            return 0, 0

        occurrences: list[_Occurrence] = []
        for finding, repository_id, pull_request_number in occurrence_rows:
            occurrences.append(
                _Occurrence(
                    finding=finding,
                    repository_id=repository_id,
                    pull_request_number=pull_request_number,
                )
            )
        keys = tuple(
            sorted(
                {
                    (
                        occurrence.repository_id,
                        occurrence.pull_request_number,
                        occurrence.finding.fingerprint,
                    )
                    for occurrence in occurrences
                }
            )
        )

        existing = {
            (row.repository_id, row.pull_request_number, row.fingerprint): row
            for row in session.scalars(
                select(FindingLifecycleRecord)
                .where(
                    tuple_(
                        FindingLifecycleRecord.repository_id,
                        FindingLifecycleRecord.pull_request_number,
                        FindingLifecycleRecord.fingerprint,
                    ).in_(keys)
                )
                .with_for_update()
            )
        }
        for occurrence in occurrences:
            finding = occurrence.finding
            key = (
                occurrence.repository_id,
                occurrence.pull_request_number,
                finding.fingerprint,
            )
            lifecycle = existing.get(key)
            if lifecycle is None:
                lifecycle = FindingLifecycleRecord(
                    repository_id=key[0],
                    pull_request_number=key[1],
                    fingerprint=key[2],
                    state=FindingLifecycleState.PRESENT.value,
                    first_seen_review_run_id=finding.review_run_id,
                    last_seen_review_run_id=finding.review_run_id,
                    previous_seen_review_run_id=None,
                    fixed_by_review_run_id=None,
                    first_seen_head_sha=finding.head_sha,
                    last_seen_head_sha=finding.head_sha,
                    last_occurrence_status="new",
                    occurrence_count=1,
                    first_seen_at=finding.created_at,
                    last_seen_at=finding.created_at,
                    fixed_at=None,
                    historical_backfilled_at=now,
                    updated_at=now,
                )
                session.add(lifecycle)
                existing[key] = lifecycle
                status = "new"
                previous_run_id: str | None = None
            elif lifecycle.last_seen_review_run_id == finding.review_run_id:
                status = lifecycle.last_occurrence_status
                previous_run_id = lifecycle.previous_seen_review_run_id
            else:
                status = (
                    "reintroduced"
                    if lifecycle.state == FindingLifecycleState.FIXED.value
                    else "still_present"
                )
                previous_run_id = lifecycle.last_seen_review_run_id
                lifecycle.previous_seen_review_run_id = previous_run_id
                lifecycle.last_seen_review_run_id = finding.review_run_id
                lifecycle.last_seen_head_sha = finding.head_sha
                lifecycle.last_occurrence_status = status
                lifecycle.occurrence_count += 1
                lifecycle.last_seen_at = finding.created_at
                lifecycle.state = FindingLifecycleState.PRESENT.value
                lifecycle.fixed_by_review_run_id = None
                lifecycle.fixed_at = None

            lifecycle.historical_backfilled_at = now
            lifecycle.updated_at = now
            finding.lifecycle_status = status
            finding.occurrence_count = lifecycle.occurrence_count
            finding.previous_review_run_id = previous_run_id
            finding.lifecycle_backfilled_at = now
        return len(keys), len(occurrences)

    @staticmethod
    def _backfill_adjudications(session: Session, batch_size: int) -> int:
        rows = list(
            session.scalars(
                select(ReviewFindingRecord)
                .where(
                    ReviewFindingRecord.adjudication_status
                    == FindingAdjudicationStatus.UNREVIEWED.value,
                    ReviewFindingRecord.reviewed_at.is_not(None),
                    ReviewFindingRecord.reviewed_by.is_not(None),
                    ReviewFindingRecord.verification_status.in_(
                        ("verified", "rejected")
                    ),
                )
                .order_by(ReviewFindingRecord.created_at, ReviewFindingRecord.id)
                .limit(batch_size)
                .with_for_update()
            )
        )
        for finding in rows:
            finding.adjudication_status = (
                FindingAdjudicationStatus.VALID.value
                if finding.verification_status == "verified"
                else FindingAdjudicationStatus.FALSE_POSITIVE.value
            )
            finding.verification_status = (
                "unverified"
                if finding.location_file is None
                else "verified"
                if finding.location_in_diff
                else "rejected"
            )
        return len(rows)

    @staticmethod
    def _backfill_evaluations(
        session: Session,
        batch_size: int,
        now: datetime,
    ) -> int:
        rows = session.execute(
            select(ReviewFindingRecord, ReviewRunRecord.repository_id)
            .join(
                ReviewRunRecord,
                ReviewRunRecord.id == ReviewFindingRecord.review_run_id,
            )
            .outerjoin(
                FindingEvaluationRecord,
                FindingEvaluationRecord.finding_id == ReviewFindingRecord.id,
            )
            .where(
                ReviewFindingRecord.adjudication_status
                != FindingAdjudicationStatus.UNREVIEWED.value,
                ReviewFindingRecord.reviewed_at.is_not(None),
                ReviewFindingRecord.reviewed_by.is_not(None),
                FindingEvaluationRecord.finding_id.is_(None),
            )
            .order_by(ReviewFindingRecord.created_at, ReviewFindingRecord.id)
            .limit(batch_size)
            # 维护命令可能由两个发布/运维进程重叠触发；跳过已被另一批次
            # 锁定的 Finding，避免重复生成同一个评测样本并撞主键。
            .with_for_update(of=ReviewFindingRecord, skip_locked=True)
        ).all()
        for finding, repository_id in rows:
            session.add(
                FindingEvaluationRecord(
                    finding_id=finding.id,
                    repository_id=repository_id,
                    category=finding.category,
                    severity=finding.severity,
                    verdict=finding.adjudication_status,
                    adjudicated_at=finding.reviewed_at,
                    adjudicated_by=finding.reviewed_by,
                    updated_at=now,
                )
            )
        return len(rows)
