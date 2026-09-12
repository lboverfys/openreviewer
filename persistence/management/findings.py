"""审查管理 findings 存储职责。"""

from hashlib import sha256

from sqlalchemy import and_, case, func, or_, select, union_all
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from domain.enums import FindingCategory, FindingEvaluationVerdict, Severity
from domain.evaluation import (
    EvaluationGatePolicy,
    EvaluationMetrics,
    evaluate_inline_gate,
)
from persistence.management.common import FindingDecisionError, _as_utc, _required_utc
from persistence.management.context import ManagementStorage
from persistence.models import (
    FindingEvaluationRecord,
    OutboxEventRecord,
    ReviewFindingRecord,
    ReviewRunRecord,
)
from persistence.resource_scope import resource_predicate
from services.rbac import ResourceScope
from services.review_management import (
    FindingCursor,
    FindingDecision,
    FindingNotFoundError,
    ReviewManagementPersistenceError,
    StoredEvaluationGate,
    StoredFinding,
    StoredFindingCounts,
)


def _load_findings(
    session: Session,
    review_run_id: str,
    *,
    limit: int,
    cursor: FindingCursor | None,
    adjudication_status: str | None,
    severity: str | None = None,
    search: str = "",
) -> tuple[tuple[StoredFinding, ...], bool]:
    query = select(
        ReviewFindingRecord.id,
        ReviewFindingRecord.fingerprint,
        ReviewFindingRecord.head_sha,
        ReviewFindingRecord.severity,
        ReviewFindingRecord.category,
        ReviewFindingRecord.title,
        ReviewFindingRecord.evidence,
        ReviewFindingRecord.impact,
        ReviewFindingRecord.suggestion,
        ReviewFindingRecord.required_test,
        ReviewFindingRecord.confidence,
        ReviewFindingRecord.verification_status,
        ReviewFindingRecord.evidence_verification_status,
        ReviewFindingRecord.evidence_verification_reason,
        ReviewFindingRecord.evidence_verified_at,
        ReviewFindingRecord.adjudication_status,
        ReviewFindingRecord.lifecycle_status,
        ReviewFindingRecord.occurrence_count,
        ReviewFindingRecord.previous_review_run_id,
        ReviewFindingRecord.location_file,
        ReviewFindingRecord.location_start_line,
        ReviewFindingRecord.location_end_line,
        ReviewFindingRecord.location_side,
        ReviewFindingRecord.location_in_diff,
        ReviewFindingRecord.location_symbol,
        ReviewFindingRecord.rule_reference,
        ReviewFindingRecord.context_references,
        ReviewFindingRecord.reviewed_at,
        ReviewFindingRecord.reviewed_by,
        ReviewFindingRecord.created_at,
    )
    query = query.where(ReviewFindingRecord.review_run_id == review_run_id)
    if adjudication_status is not None:
        query = query.where(
            ReviewFindingRecord.adjudication_status == adjudication_status
        )
    if severity:
        query = query.where(ReviewFindingRecord.severity == severity)
    if search:
        pattern = (
            "%"
            + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            + "%"
        )
        query = query.where(
            or_(
                *(
                    column.ilike(pattern, escape="\\")
                    for column in (
                        ReviewFindingRecord.title,
                        ReviewFindingRecord.category,
                        ReviewFindingRecord.location_file,
                        ReviewFindingRecord.evidence,
                    )
                )
            )
        )
    if cursor is not None:
        query = query.where(
            or_(
                ReviewFindingRecord.created_at > cursor.created_at,
                and_(
                    ReviewFindingRecord.created_at == cursor.created_at,
                    ReviewFindingRecord.id > cursor.finding_id,
                ),
            )
        )
    raw_rows = (
        session.execute(
            query.order_by(
                ReviewFindingRecord.created_at.asc(),
                ReviewFindingRecord.id.asc(),
            ).limit(limit + 1)
        )
        .mappings()
        .all()
    )
    has_more = len(raw_rows) > limit
    rows = raw_rows[:limit]
    findings = tuple(
        StoredFinding(
            id=row["id"],
            fingerprint=row["fingerprint"],
            head_sha=row["head_sha"],
            severity=row["severity"],
            category=row["category"],
            title=row["title"],
            evidence=row["evidence"],
            impact=row["impact"],
            suggestion=row["suggestion"],
            required_test=row["required_test"],
            confidence=float(row["confidence"]),
            verification_status=row["verification_status"],
            evidence_verification_status=row["evidence_verification_status"],
            evidence_verification_reason=row["evidence_verification_reason"],
            evidence_verified_at=_as_utc(row["evidence_verified_at"]),
            adjudication_status=row["adjudication_status"],
            lifecycle_status=row["lifecycle_status"],
            occurrence_count=row["occurrence_count"],
            previous_review_run_id=row["previous_review_run_id"],
            location_file=row["location_file"],
            location_start_line=row["location_start_line"],
            location_end_line=row["location_end_line"],
            location_side=row["location_side"],
            location_in_diff=bool(row["location_in_diff"]),
            location_symbol=row["location_symbol"],
            rule_reference=row["rule_reference"],
            context_references=tuple(row["context_references"] or ()),
            reviewed_at=_as_utc(row["reviewed_at"]),
            reviewed_by=row["reviewed_by"],
            created_at=_required_utc(
                row["created_at"],
                "review_finding.created_at",
            ),
        )
        for row in rows
    )
    return findings, has_more


def _load_finding_counts(
    session: Session,
    review_run_id: str,
) -> StoredFindingCounts:
    """用一次条件聚合计算整次审查的 Finding 统计，不依赖当前页。"""

    def tally(condition):
        return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)

    row = (
        session.execute(
            select(
                func.count(ReviewFindingRecord.id).label("total"),
                tally(ReviewFindingRecord.verification_status == "verified").label(
                    "location_verified"
                ),
                tally(ReviewFindingRecord.verification_status == "rejected").label(
                    "location_rejected"
                ),
                tally(ReviewFindingRecord.verification_status == "unverified").label(
                    "location_unverified"
                ),
                tally(ReviewFindingRecord.adjudication_status == "valid").label(
                    "valid"
                ),
                tally(
                    ReviewFindingRecord.adjudication_status == "false_positive"
                ).label("false_positive"),
                tally(ReviewFindingRecord.adjudication_status == "duplicate").label(
                    "duplicate"
                ),
                tally(ReviewFindingRecord.adjudication_status == "out_of_scope").label(
                    "out_of_scope"
                ),
                tally(ReviewFindingRecord.adjudication_status == "known_issue").label(
                    "known_issue"
                ),
                tally(ReviewFindingRecord.adjudication_status == "unreviewed").label(
                    "unreviewed"
                ),
                tally(ReviewFindingRecord.lifecycle_status == "new").label("new"),
                tally(ReviewFindingRecord.lifecycle_status == "still_present").label(
                    "still_present"
                ),
                tally(ReviewFindingRecord.lifecycle_status == "reintroduced").label(
                    "reintroduced"
                ),
            ).where(ReviewFindingRecord.review_run_id == review_run_id)
        )
        .mappings()
        .one()
    )
    return StoredFindingCounts(
        total=int(row["total"] or 0),
        location_verified=int(row["location_verified"] or 0),
        location_rejected=int(row["location_rejected"] or 0),
        location_unverified=int(row["location_unverified"] or 0),
        valid=int(row["valid"] or 0),
        false_positive=int(row["false_positive"] or 0),
        duplicate=int(row["duplicate"] or 0),
        out_of_scope=int(row["out_of_scope"] or 0),
        known_issue=int(row["known_issue"] or 0),
        unreviewed=int(row["unreviewed"] or 0),
        new=int(row["new"] or 0),
        still_present=int(row["still_present"] or 0),
        reintroduced=int(row["reintroduced"] or 0),
    )


def _load_evaluation_gates(
    session: Session,
    repository_id: int,
) -> tuple[StoredEvaluationGate, ...]:
    """用一次有界索引查询计算各风险域最近样本的发布准入。

    不能先对整个仓库做窗口排序再截断：评测表会随人工裁决持续增长，
    那种写法的扫描量会变成无界。每个固定风险域先在数据库内取最近
    ``recent_sample_limit`` 行，再 ``UNION ALL`` 成一条语句；因此查询次数
    始终为 O(1)，返回行数最多为风险域数量乘以样本上限。
    """

    policy = EvaluationGatePolicy()
    bounded_by_category = []
    for category in FindingCategory:
        # 先物化每个类别的 LIMIT 子查询，再拼成一条 SQL；循环只构造语句，
        # 不在循环内访问数据库，避免 N+1 查询。
        recent = (
            select(
                FindingEvaluationRecord.category,
                FindingEvaluationRecord.severity,
                FindingEvaluationRecord.verdict,
                FindingEvaluationRecord.adjudicated_at,
                FindingEvaluationRecord.finding_id,
            )
            .where(
                FindingEvaluationRecord.repository_id == repository_id,
                FindingEvaluationRecord.category == category.value,
            )
            .order_by(
                FindingEvaluationRecord.adjudicated_at.desc(),
                FindingEvaluationRecord.finding_id.desc(),
            )
            .limit(policy.recent_sample_limit)
            .subquery()
        )
        bounded_by_category.append(
            select(
                recent.c.category,
                recent.c.severity,
                recent.c.verdict,
            )
        )
    rows = session.execute(union_all(*bounded_by_category)).mappings()

    counters: dict[str, dict[str, int]] = {
        category.value: {
            "sample_count": 0,
            "valid_count": 0,
            "false_positive_count": 0,
            "duplicate_count": 0,
            "out_of_scope_count": 0,
            "known_issue_count": 0,
            "high_severity_sample_count": 0,
            "high_severity_false_positive_count": 0,
            "high_severity_duplicate_count": 0,
            "high_severity_out_of_scope_count": 0,
            "high_severity_known_issue_count": 0,
        }
        for category in FindingCategory
    }
    high_severities = {Severity.CRITICAL.value, Severity.HIGH.value}
    for row in rows:
        row_category = row["category"]
        values = counters.get(row_category)
        if values is None:
            continue
        values["sample_count"] += 1
        verdict = row["verdict"]
        is_valid = verdict == FindingEvaluationVerdict.VALID.value
        if is_valid:
            values["valid_count"] += 1
        else:
            values[f"{verdict}_count"] += 1
        if row["severity"] in high_severities:
            values["high_severity_sample_count"] += 1
            if not is_valid:
                values[f"high_severity_{verdict}_count"] += 1

    gates: list[StoredEvaluationGate] = []
    for category_value in sorted(counters):
        values = counters[category_value]
        metrics = EvaluationMetrics(
            sample_count=values["sample_count"],
            valid_count=values["valid_count"],
            false_positive_count=values["false_positive_count"],
            duplicate_count=values["duplicate_count"],
            out_of_scope_count=values["out_of_scope_count"],
            known_issue_count=values["known_issue_count"],
            high_severity_sample_count=values["high_severity_sample_count"],
            high_severity_false_positive_count=values[
                "high_severity_false_positive_count"
            ],
            high_severity_duplicate_count=values["high_severity_duplicate_count"],
            high_severity_out_of_scope_count=values["high_severity_out_of_scope_count"],
            high_severity_known_issue_count=values["high_severity_known_issue_count"],
        )
        result = evaluate_inline_gate(metrics, policy)
        gates.append(
            StoredEvaluationGate(
                category=category_value,
                sample_count=metrics.sample_count,
                valid_count=metrics.valid_count,
                false_positive_count=metrics.false_positive_count,
                duplicate_count=metrics.duplicate_count,
                out_of_scope_count=metrics.out_of_scope_count,
                known_issue_count=metrics.known_issue_count,
                rejected_count=metrics.rejected_count,
                high_severity_sample_count=(metrics.high_severity_sample_count),
                high_severity_false_positive_count=(
                    metrics.high_severity_false_positive_count
                ),
                high_severity_rejected_count=(metrics.high_severity_rejected_count),
                precision=result.precision,
                high_severity_false_positive_rate=(
                    result.high_severity_false_positive_rate
                ),
                admitted=result.admitted,
                reason=result.reason,
            )
        )
    return tuple(gates)


def review_finding(
    self: ManagementStorage,
    review_run_id: str,
    finding_id: str,
    decision: FindingDecision,
    *,
    actor: str,
    request_id: str,
    scope: ResourceScope | None = None,
) -> None:
    """保存一次人工裁决，并追加可追踪的事件。"""

    normalized_request_id = request_id.strip()
    if not normalized_request_id:
        raise ValueError("操作幂等键不能为空")
    decision_key = sha256(
        f"{review_run_id}:{finding_id}:{decision.value}:{normalized_request_id}".encode()
    ).hexdigest()
    event_key = f"review.finding.decision:{finding_id}:{decision_key}"
    with self._sessions() as session:
        try:
            # 先验证 Finding 所属运行和资源范围，再读取幂等事件；否则越权
            # 请求可能通过已存在的 event_key 观察到其他仓库的操作结果。
            scoped_finding = session.scalar(
                select(ReviewFindingRecord.id)
                .join(
                    ReviewRunRecord,
                    ReviewRunRecord.id == ReviewFindingRecord.review_run_id,
                )
                .where(
                    ReviewFindingRecord.id == finding_id,
                    ReviewFindingRecord.review_run_id == review_run_id,
                    resource_predicate(
                        scope,
                        installation_column=ReviewRunRecord.installation_id,
                        repository_column=ReviewRunRecord.repository,
                        repository_key_column=ReviewRunRecord.repository_key,
                    ),
                )
                .limit(1)
            )
            if scoped_finding is None:
                raise FindingNotFoundError("候选问题不存在")
            if (
                session.scalar(
                    select(OutboxEventRecord.id).where(
                        OutboxEventRecord.event_key == event_key
                    )
                )
                is not None
            ):
                return
            finding_row = session.execute(
                select(ReviewFindingRecord, ReviewRunRecord.repository_id)
                .join(
                    ReviewRunRecord,
                    ReviewRunRecord.id == ReviewFindingRecord.review_run_id,
                )
                .where(
                    ReviewFindingRecord.id == finding_id,
                    ReviewFindingRecord.review_run_id == review_run_id,
                    resource_predicate(
                        scope,
                        installation_column=ReviewRunRecord.installation_id,
                        repository_column=ReviewRunRecord.repository,
                        repository_key_column=ReviewRunRecord.repository_key,
                    ),
                )
                .with_for_update()
            ).one_or_none()
            if finding_row is None:
                raise FindingDecisionError("候选问题不存在")
            finding, repository_id = finding_row
            # 与任务动作相同，第一次事件查询可能早于并发事务提交；
            # Finding 行锁之后复查才能把重复请求当作成功处理。
            if (
                session.scalar(
                    select(OutboxEventRecord.id).where(
                        OutboxEventRecord.event_key == event_key
                    )
                )
                is not None
            ):
                return
            now = self._clock()
            finding.adjudication_status = decision.value
            finding.reviewed_at = now
            finding.reviewed_by = actor[:100]
            verdict = FindingEvaluationVerdict(decision.value)
            evaluation = session.get(FindingEvaluationRecord, finding_id)
            if evaluation is None:
                evaluation = FindingEvaluationRecord(
                    finding_id=finding_id,
                    repository_id=repository_id,
                    category=finding.category,
                    severity=finding.severity,
                    verdict=verdict.value,
                    adjudicated_at=now,
                    adjudicated_by=actor[:100],
                    updated_at=now,
                )
                session.add(evaluation)
            else:
                evaluation.verdict = verdict.value
                evaluation.adjudicated_at = now
                evaluation.adjudicated_by = actor[:100]
                evaluation.updated_at = now
            session.add(
                OutboxEventRecord(
                    id=str(self._uuid_factory()),
                    event_key=event_key,
                    aggregate_type="review_run",
                    aggregate_id=review_run_id,
                    event_type="review.finding.decided",
                    payload={
                        "finding_id": finding_id,
                        "decision": decision.value,
                        "actor": actor,
                    },
                    occurred_at=now,
                    publish_attempts=0,
                )
            )
            session.commit()
        except FindingNotFoundError:
            session.rollback()
            raise
        except FindingDecisionError as exc:
            session.rollback()
            raise FindingNotFoundError("候选问题不存在") from exc
        except SQLAlchemyError as exc:
            session.rollback()
            raise ReviewManagementPersistenceError(
                "finding decision could not be persisted"
            ) from exc
