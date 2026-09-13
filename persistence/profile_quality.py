"""方案质量提示复用真实评测聚合；启用时锁定评测集并重新核对证据。"""

import json
from hashlib import sha256
from typing import Literal

from sqlalchemy import select

from domain.platform import PlatformNotFoundError, ProfileQuality
from persistence.evaluation_reports import comparison_report
from persistence.models import (
    EvaluationCaseRecord,
    EvaluationDatasetRecord,
    EvaluationObservationRecord,
    RepositoryPolicyRecord,
    ReviewProfileRecord,
)
from persistence.platform_queries import repository_visible
from persistence.resource_scope import resource_predicate


def profile_quality(session, identifier, scope, dataset_id=None, *, lock=False):
    profile = session.execute(select(
        ReviewProfileRecord.repository, ReviewProfileRecord.fingerprint,
        RepositoryPolicyRecord.policy, RepositoryPolicyRecord.revision,
    ).join(RepositoryPolicyRecord,
           RepositoryPolicyRecord.repository_key == ReviewProfileRecord.repository_key)
      .where(ReviewProfileRecord.id == identifier,
             repository_visible(scope, ReviewProfileRecord.repository_key))).one_or_none()
    if profile is None:
        raise PlatformNotFoundError("审查方案不存在")
    baseline_id = profile.policy.get("review_profile_id")
    reasons = []
    report = None
    bindings = []
    if dataset_id:
        dataset_query = select(EvaluationDatasetRecord.id).where(
            EvaluationDatasetRecord.id == dataset_id,
            EvaluationDatasetRecord.repository_key == profile.repository.casefold(),
            resource_predicate(scope, installation_column=EvaluationDatasetRecord.installation_id,
                repository_column=EvaluationDatasetRecord.repository,
                repository_key_column=EvaluationDatasetRecord.repository_key),
        )
        if lock:
            dataset_query = dataset_query.with_for_update()
        if session.scalar(dataset_query) is None:
            raise PlatformNotFoundError("评测集不存在或不属于此仓库")
        report = comparison_report(session, dataset_id, scope, "validation")
        observation = EvaluationObservationRecord
        rows = session.execute(select(
            observation.variant, observation.source_snapshot["repository_policy"]["review_profile_id"].as_string().label("profile_id"),
            observation.revision, observation.snapshot_sha256,
        ).join(EvaluationCaseRecord, EvaluationCaseRecord.id == observation.case_id)
          .where(EvaluationCaseRecord.dataset_id == dataset_id,
                 EvaluationCaseRecord.split == "validation")
          .order_by(observation.id).limit(401)).all()
        if len(rows) > 400:
            raise ValueError("评测数据超过单集边界")
        bindings = [tuple(row) for row in rows]
        if not baseline_id or not rows or any(
            row.profile_id != (identifier if row.variant == "candidate" else baseline_id)
            for row in rows
        ):
            reasons.append("样本必须分别绑定当前启用方案与待启用方案，不能使用其他方案的成绩")
        if report.reference_pairs < 20:
            reasons.append("尚不足 20 组完成双人复核和参考标签确认的验收样本")
        if report.reference_pairs != report.case_count or report.pending_pairs or report.disputed_pairs:
            reasons.append("验收集仍有缺失、待复核或分歧样本")
        if not all((report.normal_count, report.known_defect_count, report.cross_file_count)):
            reasons.append("验收集需要同时包含正常变更、已知缺陷与跨文件样本")
        if report.provenance_missing_pairs or any(score.configuration_count != 1 for score in (report.baseline, report.candidate)):
            reasons.append("样本缺少独立调用的完整版本记录、包含复用结果或混用了配置")
        if report.priced_pairs != report.performance_pairs:
            reasons.append("存在未知费用，不能完整比较成本")
    else:
        reasons.append("尚未选择与两个方案绑定的真实验收评测集")
    status: Literal["unverified", "regression", "reviewed"] = "unverified" if reasons else "reviewed"
    if report:
        regressed = [name for name in ("precision", "recall") if (report.deltas.get(name) or 0) < 0]
        if regressed:
            reasons.append("候选方案的有效问题比例或已知缺陷找回率低于基线")
            if status == "reviewed":
                status = "regression"
        if (report.deltas.get("mean_estimated_cost_usd") or 0) > 0:
            reasons.append("候选方案平均估算费用增加，请结合收益判断")
    payload = {
        "profile": identifier, "fingerprint": profile.fingerprint,
        "baseline": baseline_id, "repository_revision": profile.revision,
        "bindings": bindings, "status": status, "reasons": reasons,
        "report": report.model_dump(mode="json", exclude={"generated_at"}) if report else None,
    }
    return ProfileQuality(profile_id=identifier, baseline_profile_id=baseline_id,
        status=status, evidence_token=sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        reasons=tuple(reasons), report=report)
