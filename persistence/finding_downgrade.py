"""Finding 裁决模型降级前的有界兼容数据准备。"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import String, and_, case, column, or_, select, table, update
from sqlalchemy.orm import Session, sessionmaker

_NEGATIVE_ADJUDICATIONS = (
    "false_positive",
    "duplicate",
    "out_of_scope",
    "known_issue",
)
_FINDINGS = table(
    "review_findings",
    column("id", String()),
    column("adjudication_status", String()),
    column("verification_status", String()),
    column("created_at"),
)
_EVALUATIONS = table(
    "finding_evaluations",
    column("finding_id", String()),
    column("verdict", String()),
)
_EXTENDED_EVALUATION_VERDICTS = (
    "duplicate",
    "out_of_scope",
    "known_issue",
)


@dataclass(frozen=True, slots=True)
class FindingDowngradePreparationBatch:
    findings: int
    evaluations: int

    @property
    def changed(self) -> bool:
        return self.findings > 0 or self.evaluations > 0


class SqlAlchemyFindingDowngradePreparation:
    """把新版裁决分批转换成 0026 之前可读取的二元形状。"""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def run_batch(self, batch_size: int = 100) -> FindingDowngradePreparationBatch:
        if not 1 <= batch_size <= 500:
            raise ValueError(
                "finding downgrade preparation batch size must be between 1 and 500"
            )
        with self._sessions.begin() as session:
            findings = self._prepare_findings(session, batch_size)
            evaluations = self._prepare_evaluations(session, batch_size)
            return FindingDowngradePreparationBatch(
                findings=findings,
                evaluations=evaluations,
            )

    @staticmethod
    def _prepare_findings(session: Session, batch_size: int) -> int:
        rows = session.execute(
            select(
                _FINDINGS.c.id,
                _FINDINGS.c.adjudication_status,
            )
                .where(
                    or_(
                        and_(
                            _FINDINGS.c.adjudication_status == "valid",
                            _FINDINGS.c.verification_status != "verified",
                        ),
                        and_(
                            _FINDINGS.c.adjudication_status.in_(
                                _NEGATIVE_ADJUDICATIONS
                            ),
                            _FINDINGS.c.verification_status != "rejected",
                        ),
                    )
                )
                .order_by(_FINDINGS.c.created_at, _FINDINGS.c.id)
                .limit(batch_size)
                .with_for_update()
        ).all()
        finding_ids = tuple(row.id for row in rows)
        if finding_ids:
            session.execute(
                update(_FINDINGS)
                .where(_FINDINGS.c.id.in_(finding_ids))
                .values(
                    verification_status=case(
                        (_FINDINGS.c.adjudication_status == "valid", "verified"),
                        else_="rejected",
                    )
                )
            )
        return len(rows)

    @staticmethod
    def _prepare_evaluations(session: Session, batch_size: int) -> int:
        finding_ids = tuple(
            session.scalars(
                select(_EVALUATIONS.c.finding_id)
                .where(
                    _EVALUATIONS.c.verdict.in_(
                        _EXTENDED_EVALUATION_VERDICTS
                    )
                )
                .order_by(_EVALUATIONS.c.finding_id)
                .limit(batch_size)
                .with_for_update()
            )
        )
        if finding_ids:
            session.execute(
                update(_EVALUATIONS)
                .where(_EVALUATIONS.c.finding_id.in_(finding_ids))
                .values(verdict="false_positive")
            )
        return len(finding_ids)
