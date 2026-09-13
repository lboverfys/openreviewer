"""数据库内聚合配对样本；报告不加载问题正文或逐样本执行查询。"""

from datetime import UTC, datetime
from hashlib import sha256

from sqlalchemy import and_, case, distinct, func, or_, select
from sqlalchemy.orm import Session, aliased

from domain.evaluation_workbench import (
    EvaluationComparisonReport,
    EvaluationNotFoundError,
    EvaluationScore,
    EvaluationSplit,
)
from domain.real_evaluation import summarize_evaluation_counts
from persistence.models import (
    EvaluationCaseRecord,
    EvaluationDatasetRecord,
    EvaluationObservationRecord,
)
from persistence.resource_scope import resource_predicate
from services.rbac import ResourceScope

_QUALITY_COUNTS = (
    "adjudicated_count", "valid_count", "false_positive_count", "duplicate_count",
    "out_of_scope_count", "known_issue_count", "location_assessed_count", "location_correct_count",
)


def comparison_report(
    session: Session, identifier: str, scope: ResourceScope, split: EvaluationSplit,
) -> EvaluationComparisonReport:
    baseline = aliased(EvaluationObservationRecord, name="baseline")
    candidate = aliased(EvaluationObservationRecord, name="candidate")
    both = and_(baseline.id.is_not(None), candidate.id.is_not(None))
    paired = and_(both, baseline.source_run_id != candidate.source_run_id)
    quality = and_(
        paired, baseline.assessment_status == "complete",
        candidate.assessment_status == "complete",
    )
    referenced = and_(quality, EvaluationCaseRecord.reference_status == "confirmed")
    priced = and_(paired, baseline.estimated_cost_microusd.is_not(None),
                  candidate.estimated_cost_microusd.is_not(None))

    def total(condition, expression=1):
        return func.coalesce(func.sum(case((condition, expression), else_=0)), 0)

    columns = [
        EvaluationDatasetRecord.id, EvaluationDatasetRecord.name,
        EvaluationDatasetRecord.repository, EvaluationDatasetRecord.revision,
        func.count(EvaluationCaseRecord.id).label("case_count"),
        func.coalesce(func.sum(EvaluationCaseRecord.revision), 0).label("case_revisions"),
        total(paired).label("performance_pairs"), total(quality).label("quality_pairs"),
        total(referenced).label("reference_pairs"), total(priced).label("priced_pairs"),
        total(and_(EvaluationCaseRecord.id.is_not(None), baseline.id.is_(None))).label("missing_baseline"),
        total(and_(EvaluationCaseRecord.id.is_not(None), candidate.id.is_(None))).label("missing_candidate"),
        total(and_(both, baseline.source_run_id == candidate.source_run_id)).label("identical_run_pairs"),
        total(and_(paired, or_(baseline.assessment_status == "disputed",
                              candidate.assessment_status == "disputed"))).label("disputed_pairs"),
        total(and_(paired, or_(baseline.assessment_status.in_(("pending", "partial")),
                              candidate.assessment_status.in_(("pending", "partial"))))).label("pending_pairs"),
        total(and_(paired, or_(baseline.provenance_complete.is_(False),
                              candidate.provenance_complete.is_(False)))).label("provenance_missing_pairs"),
        total(referenced, func.coalesce(EvaluationCaseRecord.reference_count, 0)).label("reference_expected_count"),
    ]
    for kind in ("normal", "known_defect", "cross_file"):
        columns.append(total(EvaluationCaseRecord.kind == kind).label(f"{kind}_count"))
    for prefix, observation in (("baseline", baseline), ("candidate", candidate)):
        columns.append(total(quality, observation.finding_count).label(f"{prefix}_finding_count"))
        for key in _QUALITY_COUNTS:
            columns.append(total(quality, func.coalesce(observation.metrics[key].as_integer(), 0)).label(f"{prefix}_{key}"))
        for key in ("reference_true_positive_count", "reference_unexpected_valid_count"):
            columns.append(total(referenced, func.coalesce(observation.metrics[key].as_integer(), 0)).label(f"{prefix}_{key}"))
        for key in ("turnaround_ms", "model_duration_ms", "input_tokens", "output_tokens"):
            columns.append(total(paired, getattr(observation, key)).label(f"{prefix}_{key}"))
        columns.append(total(priced, observation.estimated_cost_microusd).label(f"{prefix}_cost"))
        columns.append(func.count(distinct(case((paired, observation.configuration_fingerprint), else_=None))).label(f"{prefix}_configuration_count"))
    statement = (select(*columns).select_from(EvaluationDatasetRecord)
        .outerjoin(EvaluationCaseRecord, and_(
            EvaluationCaseRecord.dataset_id == EvaluationDatasetRecord.id,
            EvaluationCaseRecord.split == split,
        ))
        .outerjoin(baseline, and_(baseline.case_id == EvaluationCaseRecord.id, baseline.variant == "baseline"))
        .outerjoin(candidate, and_(candidate.case_id == EvaluationCaseRecord.id, candidate.variant == "candidate"))
        .where(EvaluationDatasetRecord.id == identifier, resource_predicate(
            scope, installation_column=EvaluationDatasetRecord.installation_id,
            repository_column=EvaluationDatasetRecord.repository,
            repository_key_column=EvaluationDatasetRecord.repository_key,
        ))
        .group_by(EvaluationDatasetRecord.id, EvaluationDatasetRecord.name,
                  EvaluationDatasetRecord.repository, EvaluationDatasetRecord.revision))
    row = session.execute(statement).mappings().one_or_none()
    if row is None:
        raise EvaluationNotFoundError("评测集不存在")
    def number(key: str) -> int:
        return int(row[key] or 0)
    def mean(value: int, count: int) -> float | None:
        return round(value / count, 3) if count else None
    scores: dict[str, EvaluationScore] = {}
    for prefix in ("baseline", "candidate"):
        counts = {key: number(f"{prefix}_{key}") for key in _QUALITY_COUNTS}
        counts.update(
            sample_count=number("quality_pairs"), finding_count=number(f"{prefix}_finding_count"),
            reference_sample_count=number("reference_pairs"),
            reference_expected_count=number("reference_expected_count"),
            reference_true_positive_count=number(f"{prefix}_reference_true_positive_count"),
            reference_unexpected_valid_count=number(f"{prefix}_reference_unexpected_valid_count"),
        )
        counts["reference_false_negative_count"] = counts["reference_expected_count"] - counts["reference_true_positive_count"]
        calculated = summarize_evaluation_counts(counts)
        payload = {key: value for key, value in calculated.items() if key in EvaluationScore.model_fields}
        payload.update(
            mean_turnaround_ms=mean(number(f"{prefix}_turnaround_ms"), number("performance_pairs")),
            mean_model_duration_ms=mean(number(f"{prefix}_model_duration_ms"), number("performance_pairs")),
            mean_estimated_cost_usd=(
                round(number(f"{prefix}_cost") / number("priced_pairs") / 1_000_000, 8)
                if number("priced_pairs") else None
            ),
            input_tokens=number(f"{prefix}_input_tokens"),
            output_tokens=number(f"{prefix}_output_tokens"),
            configuration_count=number(f"{prefix}_configuration_count"),
        )
        scores[prefix] = EvaluationScore.model_validate(payload)
    notices = []
    if split == "tuning":
        notices.append("当前为调参集报告；最终效果请使用独立验收集验证")
    if not number("quality_pairs"):
        notices.append("尚无双方完成双人复核且结论一致的配对样本，效果指标暂不计算")
    if number("missing_baseline") or number("missing_candidate"):
        notices.append("未配对样本单列，不参与基线与候选对比")
    if number("provenance_missing_pairs"):
        notices.append("部分运行缺少独立调用的完整版本记录，或包含跨提交复用结果")
    if any(score.configuration_count > 1 for score in scores.values()):
        notices.append("同一分组包含多种配置记录，请结合样本版本信息解释整体差异")
    if number("performance_pairs") > number("priced_pairs"):
        notices.append("费用只比较双方价格均已记录的配对样本，未知费用不视为零")
    deltas: dict[str, float | None] = {}
    for key in ("precision", "recall", "mean_turnaround_ms", "mean_model_duration_ms", "mean_estimated_cost_usd"):
        left, right = getattr(scores["baseline"], key), getattr(scores["candidate"], key)
        deltas[key] = round(right - left, 8) if left is not None and right is not None else None
    return EvaluationComparisonReport(
        metric_scope="paired_review_workflow",
        dataset_id=identifier, dataset_name=row["name"], repository=row["repository"],
        split=split, generated_at=datetime.now(UTC),
        data_version=sha256(f"{identifier}:{split}:{row['revision']}:{row['case_revisions']}".encode()).hexdigest()[:20],
        **{key: number(key) for key in (
            "case_count", "normal_count", "known_defect_count", "cross_file_count",
            "performance_pairs", "quality_pairs", "reference_pairs", "priced_pairs",
            "missing_baseline", "missing_candidate", "pending_pairs", "disputed_pairs",
            "identical_run_pairs", "provenance_missing_pairs",
        )},
        baseline=scores["baseline"], candidate=scores["candidate"],
        deltas=deltas, notices=tuple(notices),
    )
