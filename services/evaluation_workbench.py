"""评测集、独立审查快照与登录成员的双人复核用例。"""

import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, insert, select, update
from sqlalchemy.orm import Load, Session, aliased, sessionmaker

from domain.evaluation_workbench import (
    MAX_EVALUATION_CASES,
    BallotSummary,
    EvaluationArchive,
    EvaluationAuditView,
    EvaluationBallot,
    EvaluationCaseDetail,
    EvaluationCaseView,
    EvaluationChange,
    EvaluationConflictError,
    EvaluationDatasetCreate,
    EvaluationDatasetView,
    EvaluationFindingView,
    EvaluationImportResult,
    EvaluationNotFoundError,
    EvaluationRunOption,
    EvaluationSource,
    EvaluationSourceMetadata,
    EvaluationSplit,
    EvaluationVariant,
    FindingReview,
    FindingReviewWrite,
    ObservationDetail,
    ObservationImport,
    ObservationReplace,
    ObservationView,
    ReferenceDefect,
    ReferenceReview,
    ReferenceReviewWrite,
    ReferenceUpdate,
    assessment_metrics,
    reference_status,
)
from domain.pagination import CursorPage, decode_cursor, encode_cursor
from domain.security import redact_text
from persistence.evaluation_reports import comparison_report
from persistence.evaluation_sources import capture_review_sources
from persistence.models import (
    EvaluationCaseRecord,
    EvaluationDatasetRecord,
    EvaluationObservationRecord,
    ModelCallRecord,
    OutboxEventRecord,
    PullRequestVersionRecord,
    ReviewPlanRecord,
    ReviewRunRecord,
)
from persistence.pagination import apply_cursor
from persistence.resource_scope import resource_predicate
from services.rbac import ResourceScope

_DATASET_COLUMNS = (
    "id", "name", "repository", "case_count", "revision", "archived_at",
    "created_by", "created_at", "updated_at",
)
_CASE_COLUMNS = (
    "id", "dataset_id", "pull_request_number", "head_sha", "title", "split", "kind",
    "reference_status", "reference_count", "revision", "created_at", "updated_at",
)
_OBSERVATION_COLUMNS = (
    "id", "variant", "source_run_id", "snapshot_sha256", "model_label",
    "provenance_complete", "finding_count", "assessment_status", "revision",
    "captured_by", "created_at", "updated_at",
)


def dataset_scope(scope: ResourceScope):
    return resource_predicate(
        scope, installation_column=EvaluationDatasetRecord.installation_id,
        repository_column=EvaluationDatasetRecord.repository,
        repository_key_column=EvaluationDatasetRecord.repository_key,
    )


def _digest(value: object) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode()).hexdigest()


def _dataset_view(record: EvaluationDatasetRecord) -> EvaluationDatasetView:
    return EvaluationDatasetView.model_validate(record, from_attributes=True)


def _observation_view(record: EvaluationObservationRecord) -> ObservationView:
    return ObservationView.model_validate(record, from_attributes=True)


def _observation_values(source: EvaluationSource) -> dict[str, Any]:
    snapshot = source.model_dump(mode="json")
    configuration = {
        "models": [model.model_dump(mode="json") for model in source.models],
        "configuration_revision": source.configuration_revision,
        "knowledge_sources": (source.repository_policy or {}).get("knowledge_sources"),
        "retrieval": sorted({
            (item.embedding_model, item.strategy or "unknown") for item in source.retrieval
        }),
    }
    return {
        "source_run_id": source.review_run_id, "source_snapshot": snapshot,
        "snapshot_sha256": _digest(snapshot), "configuration_fingerprint": _digest(configuration),
        "model_label": " / ".join(dict.fromkeys(model.model for model in source.models))[:600],
        "provenance_complete": all(
            model.context_recorded and model.application_revision is not None and model.reused_from_run_id is None
            for model in source.models
        ),
        "finding_count": len(source.findings), "input_tokens": source.input_tokens,
        "output_tokens": source.output_tokens, "model_duration_ms": source.model_duration_ms,
        "turnaround_ms": source.turnaround_ms, "estimated_cost_microusd": source.estimated_cost_microusd,
        "ballots": [], "metrics": {}, "assessment_status": "pending",
    }


def _audit(session: Session, dataset_id: str, actor: str, action: str, payload: dict[str, object]):
    identifier = str(uuid4())
    session.add(OutboxEventRecord(
        id=identifier, event_key=f"evaluation.{action}:{identifier}",
        aggregate_type="evaluation", aggregate_id=dataset_id,
        event_type=f"evaluation.{action}", occurred_at=datetime.now(UTC),
        payload={"actor": actor, **payload},
    ))


class EvaluationWorkbench:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def sources(
        self, scope: ResourceScope, *, limit: int = 10, cursor: str | None = None,
        dataset_id: str | None = None, case_id: str | None = None,
    ) -> CursorPage[EvaluationRunOption]:
        statement = (select(
            ReviewRunRecord.id.label("review_run_id"), ReviewRunRecord.repository,
            ReviewRunRecord.pull_request_number, ReviewRunRecord.head_sha,
            PullRequestVersionRecord.title, ModelCallRecord.finding_count,
            ModelCallRecord.model, ReviewPlanRecord.model_review_completed_at.label("completed_at"),
            ReviewRunRecord.created_at,
        ).select_from(ReviewRunRecord)
          .join(ReviewPlanRecord, ReviewPlanRecord.review_run_id == ReviewRunRecord.id)
          .join(ModelCallRecord, ModelCallRecord.review_plan_id == ReviewPlanRecord.id)
          .outerjoin(PullRequestVersionRecord,
                     PullRequestVersionRecord.id == ReviewPlanRecord.pull_request_version_id)
          .where(
              ReviewRunRecord.coverage_status == "complete",
              ModelCallRecord.status == "succeeded",
              ReviewPlanRecord.model_review_completed_at.is_not(None),
              resource_predicate(scope, installation_column=ReviewRunRecord.installation_id,
                                 repository_column=ReviewRunRecord.repository,
                                 repository_key_column=ReviewRunRecord.repository_key),
          ))
        with self.sessions() as session:
            if case_id is not None:
                row = session.execute(select(
                    EvaluationCaseRecord.pull_request_number, EvaluationCaseRecord.head_sha,
                    EvaluationCaseRecord.dataset_id,
                ).join(EvaluationDatasetRecord, EvaluationDatasetRecord.id == EvaluationCaseRecord.dataset_id)
                  .where(EvaluationCaseRecord.id == case_id, dataset_scope(scope))).one_or_none()
                if row is None:
                    raise EvaluationNotFoundError("评测样本不存在")
                if dataset_id is not None and dataset_id != row.dataset_id:
                    raise ValueError("样本不属于当前评测集")
                dataset_id = row.dataset_id
                statement = statement.where(
                    ReviewRunRecord.pull_request_number == row.pull_request_number,
                    ReviewRunRecord.head_sha == row.head_sha,
                )
            if dataset_id is not None:
                dataset = self._dataset(session, dataset_id, scope)
                statement = statement.where(
                    ReviewRunRecord.repository_key == dataset.repository_key,
                    ReviewRunRecord.installation_id == dataset.installation_id,
                )
            rows = session.execute(apply_cursor(
                statement, ReviewRunRecord.created_at, ReviewRunRecord.id, cursor,
            ).limit(limit + 1)).mappings().all()
        items = tuple(EvaluationRunOption.model_validate(row) for row in rows[:limit])
        return CursorPage(items=items, next_cursor=(
            encode_cursor(items[-1].created_at, items[-1].review_run_id)
            if len(rows) > limit else None
        ))

    def report(self, identifier: str, scope: ResourceScope, split: EvaluationSplit = "validation"):
        with self.sessions() as session:
            return comparison_report(session, identifier, scope, split)

    def audits(
        self, identifier: str, scope: ResourceScope, *, limit: int = 10,
        cursor: str | None = None,
    ) -> CursorPage[EvaluationAuditView]:
        with self.sessions() as session:
            self._dataset(session, identifier, scope)
            statement = select(
                OutboxEventRecord.id, OutboxEventRecord.event_type,
                OutboxEventRecord.payload, OutboxEventRecord.occurred_at,
            ).where(OutboxEventRecord.aggregate_type == "evaluation",
                    OutboxEventRecord.aggregate_id == identifier)
            rows = session.execute(apply_cursor(
                statement, OutboxEventRecord.occurred_at, OutboxEventRecord.id, cursor,
            ).limit(limit + 1)).mappings().all()
        items = tuple(EvaluationAuditView.model_validate(row) for row in rows[:limit])
        return CursorPage(items=items, next_cursor=(
            encode_cursor(items[-1].occurred_at, items[-1].id) if len(rows) > limit else None
        ))

    def datasets(
        self, scope: ResourceScope, *, limit: int = 10, cursor: str | None = None,
        include_archived: bool = False,
    ) -> CursorPage[EvaluationDatasetView]:
        statement = select(*(getattr(EvaluationDatasetRecord, key) for key in _DATASET_COLUMNS)).where(dataset_scope(scope))
        if not include_archived:
            statement = statement.where(EvaluationDatasetRecord.archived_at.is_(None))
        statement = apply_cursor(statement, EvaluationDatasetRecord.created_at,
                                 EvaluationDatasetRecord.id, cursor).limit(limit + 1)
        with self.sessions() as session:
            rows = session.execute(statement).mappings().all()
        items = tuple(EvaluationDatasetView.model_validate(row) for row in rows[:limit])
        return CursorPage(items=items, next_cursor=(
            encode_cursor(items[-1].created_at, items[-1].id) if len(rows) > limit else None
        ))

    @staticmethod
    def _dataset(
        session: Session, identifier: str, scope: ResourceScope, *, lock: bool = False,
    ) -> EvaluationDatasetRecord:
        statement = select(EvaluationDatasetRecord).where(
            EvaluationDatasetRecord.id == identifier, dataset_scope(scope),
        )
        if lock:
            statement = statement.with_for_update()
        record = session.scalar(statement)
        if record is None:
            raise EvaluationNotFoundError("评测集不存在")
        return record

    def dataset(self, identifier: str, scope: ResourceScope) -> EvaluationDatasetView:
        with self.sessions() as session:
            return _dataset_view(self._dataset(session, identifier, scope))

    def create_dataset(
        self, draft: EvaluationDatasetCreate, request_id: str, actor: str, scope: ResourceScope,
    ) -> EvaluationDatasetView:
        request_key = _digest([actor, request_id])
        fingerprint = _digest(draft.model_dump(mode="json"))
        now = datetime.now(UTC)
        with self.sessions() as session:
            existing = session.scalar(select(EvaluationDatasetRecord).where(
                EvaluationDatasetRecord.request_key == request_key, dataset_scope(scope),
            ))
            if existing is not None:
                if existing.request_fingerprint != fingerprint:
                    raise EvaluationConflictError("同一请求标识不能用于不同评测集")
                return _dataset_view(existing)
            sources = capture_review_sources(session, draft.review_run_ids, scope, now)
            first = next(iter(sources.values()))
            record = EvaluationDatasetRecord(
                id=str(uuid4()), name=redact_text(draft.name),
                installation_id=first.installation_id, repository_id=first.repository_id,
                repository=first.repository, repository_key=first.repository_key,
                request_key=request_key, request_fingerprint=fingerprint,
                case_count=0, revision=1, created_by=actor, created_at=now, updated_at=now,
            )
            session.add(record)
            session.flush()
            self._import(session, record, sources, draft, actor, now)
            _audit(session, record.id, actor, "dataset_created", {"name": record.name})
            session.commit()
            return _dataset_view(record)

    def import_observations(
        self, identifier: str, draft: ObservationImport, actor: str, scope: ResourceScope,
    ) -> EvaluationImportResult:
        now = datetime.now(UTC)
        with self.sessions() as session:
            # 统一锁顺序：源运行共享锁 -> 评测集 -> 样本 -> 观察记录。
            sources = capture_review_sources(session, draft.review_run_ids, scope, now)
            dataset = self._dataset(session, identifier, scope, lock=True)
            if dataset.archived_at is not None:
                raise EvaluationConflictError("评测集已归档，请先恢复")
            result = self._import(session, dataset, sources, draft, actor, now)
            session.commit()
            return result

    def _import(self, session, dataset, sources, draft, actor, now) -> EvaluationImportResult:
        numbers = [source.pull_request_number for source in sources.values()]
        if len(set(numbers)) != len(numbers):
            raise ValueError("同一分组中每个 PR 只能选择一条运行")
        if any(
            source.repository_id != dataset.repository_id
            or source.installation_id != dataset.installation_id
            or source.repository_key != dataset.repository_key
            for source in sources.values()
        ):
            raise ValueError("同一评测集只能收录同一仓库和安装范围")
        rows = session.execute(select(
            EvaluationCaseRecord, EvaluationObservationRecord.variant,
            EvaluationObservationRecord.source_run_id,
        ).outerjoin(EvaluationObservationRecord,
                    EvaluationObservationRecord.case_id == EvaluationCaseRecord.id)
          .where(EvaluationCaseRecord.dataset_id == dataset.id,
                 EvaluationCaseRecord.pull_request_number.in_(numbers))
          .options(Load(EvaluationCaseRecord).load_only(
              EvaluationCaseRecord.id, EvaluationCaseRecord.dataset_id,
              EvaluationCaseRecord.pull_request_number, EvaluationCaseRecord.head_sha,
              EvaluationCaseRecord.split, EvaluationCaseRecord.revision,
              EvaluationCaseRecord.updated_at, raiseload=True,
          )).with_for_update(of=EvaluationCaseRecord).limit(60)).all()
        cases = {record.pull_request_number: record for record, _variant, _run in rows}
        existing = {(record.id, variant): run_id for record, variant, run_id in rows if variant is not None}
        new_cases: list[dict[str, object]] = []
        new_observations: list[dict[str, object]] = []
        selected_ids: list[str] = []
        for source in sources.values():
            record = cases.get(source.pull_request_number)
            if record is not None:
                if record.head_sha != source.head_sha:
                    raise EvaluationConflictError("同一 PR 的基线和候选必须使用同一提交 SHA")
                if record.split != draft.split:
                    raise EvaluationConflictError("同一 PR 不能同时进入调参集和验收集")
                identifier = record.id
            else:
                identifier = str(uuid4())
                new_cases.append({
                    "id": identifier, "dataset_id": dataset.id,
                    "pull_request_number": source.pull_request_number, "head_sha": source.head_sha,
                    "title": source.title, "split": draft.split, "kind": draft.kind,
                    "reference_defects": None, "reference_reviews": [], "reference_status": "pending",
                    "reference_count": None, "revision": 1, "created_at": now, "updated_at": now,
                })
            selected_ids.append(identifier)
            current = existing.get((identifier, draft.variant))
            if current is not None:
                if current != source.review_run_id:
                    raise EvaluationConflictError("该分组已有观察结果，请在样本详情中更换运行")
                continue
            opposite = "candidate" if draft.variant == "baseline" else "baseline"
            if existing.get((identifier, opposite)) == source.review_run_id:
                raise EvaluationConflictError("同一条运行不能同时作为基线和候选")
            new_observations.append({
                "id": str(uuid4()), "case_id": identifier, "variant": draft.variant,
                **_observation_values(source), "revision": 1, "captured_by": actor,
                "created_at": now, "updated_at": now,
            })
            if record is not None:
                record.revision += 1
                record.updated_at = now
        if dataset.case_count + len(new_cases) > MAX_EVALUATION_CASES:
            raise ValueError("每个评测集最多收录 200 个 PR")
        if new_cases:
            session.execute(insert(EvaluationCaseRecord).execution_options(render_nulls=True), new_cases)
        if new_observations:
            session.execute(insert(EvaluationObservationRecord).execution_options(render_nulls=True), new_observations)
            dataset.case_count += len(new_cases)
            dataset.revision += 1
            dataset.updated_at = now
            _audit(session, dataset.id, actor, "observations_imported", {
                "variant": draft.variant, "split": draft.split,
                "review_run_ids": list(sources), "case_ids": selected_ids,
            })
        return EvaluationImportResult(
            dataset_id=dataset.id, case_ids=tuple(selected_ids), imported=len(new_observations),
        )

    @staticmethod
    def _case_query(scope: ResourceScope, *, detail: bool = False):
        baseline = aliased(EvaluationObservationRecord, name="baseline")
        candidate = aliased(EvaluationObservationRecord, name="candidate")
        columns = [getattr(EvaluationCaseRecord, key) for key in _CASE_COLUMNS]
        columns.append(EvaluationDatasetRecord.repository)
        if detail:
            columns.extend([EvaluationCaseRecord.reference_defects, EvaluationCaseRecord.reference_reviews])
        for prefix, observation in (("baseline", baseline), ("candidate", candidate)):
            columns.extend(getattr(observation, key).label(f"{prefix}_{key}") for key in _OBSERVATION_COLUMNS)
        return (select(*columns).select_from(EvaluationCaseRecord)
            .join(EvaluationDatasetRecord, EvaluationDatasetRecord.id == EvaluationCaseRecord.dataset_id)
            .outerjoin(baseline, and_(baseline.case_id == EvaluationCaseRecord.id, baseline.variant == "baseline"))
            .outerjoin(candidate, and_(candidate.case_id == EvaluationCaseRecord.id, candidate.variant == "candidate"))
            .where(dataset_scope(scope)))

    @staticmethod
    def _case_view(row, *, detail: bool = False):
        payload = {key: row[key] for key in (*_CASE_COLUMNS, "repository")}
        for variant in ("baseline", "candidate"):
            payload[variant] = (
                {key: row[f"{variant}_{key}"] for key in _OBSERVATION_COLUMNS}
                if row[f"{variant}_id"] is not None else None
            )
        if detail:
            payload.update(reference_defects=row["reference_defects"], reference_reviews=row["reference_reviews"])
            return EvaluationCaseDetail.model_validate(payload)
        return EvaluationCaseView.model_validate(payload)

    def cases(
        self, identifier: str, scope: ResourceScope, *, limit: int = 10,
        cursor: str | None = None, split: str | None = None,
    ) -> CursorPage[EvaluationCaseView]:
        with self.sessions() as session:
            self._dataset(session, identifier, scope)
            statement = self._case_query(scope).where(EvaluationCaseRecord.dataset_id == identifier)
            if split is not None:
                statement = statement.where(EvaluationCaseRecord.split == split)
            rows = session.execute(apply_cursor(
                statement, EvaluationCaseRecord.created_at, EvaluationCaseRecord.id, cursor,
            ).limit(limit + 1)).mappings().all()
        items = tuple(self._case_view(row) for row in rows[:limit])
        return CursorPage(items=items, next_cursor=(
            encode_cursor(items[-1].created_at, items[-1].id) if len(rows) > limit else None
        ))

    def case(self, identifier: str, scope: ResourceScope) -> EvaluationCaseDetail:
        with self.sessions() as session:
            row = session.execute(self._case_query(scope, detail=True).where(
                EvaluationCaseRecord.id == identifier,
            )).mappings().one_or_none()
            if row is None:
                raise EvaluationNotFoundError("评测样本不存在")
            return self._case_view(row, detail=True)

    @staticmethod
    def _locked_case(session: Session, identifier: str, scope: ResourceScope) -> EvaluationCaseRecord:
        dataset = session.scalar(select(EvaluationDatasetRecord)
            .join(EvaluationCaseRecord, EvaluationCaseRecord.dataset_id == EvaluationDatasetRecord.id)
            .where(EvaluationCaseRecord.id == identifier, dataset_scope(scope))
            .with_for_update(read=True, of=EvaluationDatasetRecord))
        if dataset is None:
            raise EvaluationNotFoundError("评测样本不存在")
        if dataset.archived_at is not None:
            raise EvaluationConflictError("评测集已归档，请先恢复")
        return session.scalars(select(EvaluationCaseRecord).where(
            EvaluationCaseRecord.id == identifier,
        ).with_for_update()).one()

    @staticmethod
    def _observation(
        session: Session, case_id: str, variant: EvaluationVariant,
        scope: ResourceScope, *, lock: bool = False,
    ) -> EvaluationObservationRecord:
        statement = (select(EvaluationObservationRecord)
            .join(EvaluationCaseRecord, EvaluationCaseRecord.id == EvaluationObservationRecord.case_id)
            .join(EvaluationDatasetRecord, EvaluationDatasetRecord.id == EvaluationCaseRecord.dataset_id)
            .where(EvaluationObservationRecord.case_id == case_id,
                   EvaluationObservationRecord.variant == variant, dataset_scope(scope))
            .options(Load(EvaluationObservationRecord).load_only(
                *(getattr(EvaluationObservationRecord, key) for key in _OBSERVATION_COLUMNS),
                EvaluationObservationRecord.case_id, EvaluationObservationRecord.source_snapshot,
                EvaluationObservationRecord.ballots, raiseload=True,
            )))
        if lock:
            statement = statement.with_for_update(of=EvaluationObservationRecord)
        record = session.scalar(statement)
        if record is None:
            raise EvaluationNotFoundError("该分组尚未收录审查结果")
        return record

    @staticmethod
    def _observation_detail(record: EvaluationObservationRecord) -> ObservationDetail:
        ballots = tuple(EvaluationBallot.model_validate(item) for item in record.ballots)
        changes = record.source_snapshot.get("changes", [])
        return ObservationDetail(
            observation=_observation_view(record),
            source=EvaluationSourceMetadata.model_validate({
                **{key: value for key, value in record.source_snapshot.items()
                   if key not in {"findings", "changes"}},
                "change_count": len(changes) if isinstance(changes, list) else 0,
            }),
            ballots=tuple(BallotSummary(
                reviewer=item.reviewer, decision_count=len(item.decisions),
                submitted_at=item.submitted_at, updated_at=item.updated_at,
            ) for item in ballots),
        )

    def observation(self, case_id: str, variant: EvaluationVariant, scope: ResourceScope) -> ObservationDetail:
        with self.sessions() as session:
            return self._observation_detail(self._observation(session, case_id, variant, scope))

    def findings(
        self, case_id: str, variant: EvaluationVariant, scope: ResourceScope,
        *, limit: int = 10, cursor: str | None = None,
    ) -> CursorPage[EvaluationFindingView]:
        with self.sessions() as session:
            record = self._observation(session, case_id, variant, scope)
            offset = 0
            if cursor:
                _date, marker = decode_cursor(cursor)
                digest, separator, position = marker.partition(":")
                if not separator or not position.isdecimal():
                    raise ValueError("分页游标无效")
                if digest != record.snapshot_sha256:
                    raise EvaluationConflictError("观察结果已更换，请返回第一页")
                offset = int(position)
                if not 0 <= offset <= 500:
                    raise ValueError("分页游标超出范围")
            source = EvaluationSource.model_validate(record.source_snapshot)
            ballots = tuple(EvaluationBallot.model_validate(item) for item in record.ballots)
            items = tuple(EvaluationFindingView(
                finding=finding, reviews=tuple(FindingReview(
                    reviewer=ballot.reviewer, decision=ballot.decisions[finding.id],
                    submitted_at=ballot.submitted_at,
                ) for ballot in ballots if finding.id in ballot.decisions),
            ) for finding in source.findings[offset:offset + limit])
            return CursorPage(items=items, next_cursor=(
                encode_cursor(record.created_at, f"{record.snapshot_sha256}:{offset + limit}")
                if offset + limit < len(source.findings) else None
            ))

    def archive(
        self, identifier: str, draft: EvaluationArchive, actor: str, scope: ResourceScope,
    ) -> EvaluationDatasetView:
        with self.sessions() as session:
            record = self._dataset(session, identifier, scope, lock=True)
            self._revision(record.revision, draft.expected_revision)
            record.archived_at = datetime.now(UTC) if draft.archived else None
            record.revision += 1
            record.updated_at = datetime.now(UTC)
            _audit(session, record.id, actor, "dataset_archived" if draft.archived else "dataset_restored", {})
            session.commit()
            return _dataset_view(record)

    def changes(
        self, case_id: str, variant: EvaluationVariant, scope: ResourceScope,
        *, limit: int = 10, cursor: str | None = None,
    ) -> CursorPage[EvaluationChange]:
        with self.sessions() as session:
            record = self._observation(session, case_id, variant, scope)
            offset = 0
            if cursor:
                _date, marker = decode_cursor(cursor)
                digest, separator, position = marker.partition(":")
                if not separator or not position.isdecimal() or not 0 <= int(position) <= 500:
                    raise ValueError("分页游标无效")
                if digest != record.snapshot_sha256:
                    raise EvaluationConflictError("观察结果已更换，请返回第一页")
                offset = int(position)
            changes = record.source_snapshot.get("changes", [])
            if not isinstance(changes, list):
                raise ValueError("变更快照格式无效")
            items = tuple(EvaluationChange.model_validate(item) for item in changes[offset:offset + limit])
            return CursorPage(items=items, next_cursor=(
                encode_cursor(record.created_at, f"{record.snapshot_sha256}:{offset + limit}")
                if offset + limit < len(changes) else None
            ))

    def update_reference(
        self, identifier: str, draft: ReferenceUpdate, actor: str, scope: ResourceScope,
    ) -> EvaluationCaseDetail:
        now = datetime.now(UTC)
        with self.sessions() as session:
            record = self._locked_case(session, identifier, scope)
            self._revision(record.revision, draft.expected_revision)
            reviewed = session.scalar(select(EvaluationObservationRecord.id).where(
                EvaluationObservationRecord.case_id == identifier,
                EvaluationObservationRecord.assessment_status != "pending",
            ).limit(1))
            if (record.reference_reviews or reviewed is not None) and not draft.reset_reviews:
                raise EvaluationConflictError("修改参考会清空本 PR 的复核记录，请明确选择重置后保存")
            record.reference_defects = (
                [item.model_dump(mode="json") for item in draft.reference_defects]
                if draft.reference_defects is not None else None
            )
            record.reference_count = (
                len(draft.reference_defects) if draft.reference_defects is not None else None
            )
            record.reference_reviews = []
            record.reference_status = "pending"
            if draft.kind is not None:
                record.kind = draft.kind
            record.revision += 1
            record.updated_at = now
            session.execute(update(EvaluationObservationRecord).where(
                EvaluationObservationRecord.case_id == identifier,
            ).values(
                ballots=[], metrics={}, assessment_status="pending",
                revision=EvaluationObservationRecord.revision + 1, updated_at=now,
            ))
            _audit(session, record.dataset_id, actor, "reference_updated", {
                "case_id": identifier, "reference_count": record.reference_count,
                "revision": record.revision, "reviews_reset": bool(reviewed or draft.reset_reviews),
            })
            session.commit()
        return self.case(identifier, scope)

    def review_reference(
        self, identifier: str, draft: ReferenceReviewWrite, actor: str, scope: ResourceScope,
    ) -> EvaluationCaseDetail:
        now = datetime.now(UTC)
        with self.sessions() as session:
            record = self._locked_case(session, identifier, scope)
            self._revision(record.revision, draft.expected_revision)
            if record.reference_defects is None:
                raise ValueError("请先填写参考缺陷；确认无缺陷时保存空列表")
            reviews = [ReferenceReview.model_validate(item) for item in record.reference_reviews]
            index = next((number for number, item in enumerate(reviews) if item.reviewer.casefold() == actor.casefold()), None)
            if index is None and len(reviews) >= 2:
                raise EvaluationConflictError("参考标签已由两位成员认领，可由原复核人修改或重置")
            review = ReferenceReview(reviewer=actor, agrees=draft.agrees,
                                     note=redact_text(draft.note), reviewed_at=now)
            if index is None:
                reviews.append(review)
            else:
                reviews[index] = review
            record.reference_reviews = [item.model_dump(mode="json") for item in reviews]
            record.reference_status = reference_status(tuple(reviews))
            record.revision += 1
            record.updated_at = now
            _audit(session, record.dataset_id, actor, "reference_reviewed", {
                "case_id": identifier, "agrees": draft.agrees, "revision": record.revision,
            })
            session.commit()
        return self.case(identifier, scope)

    @staticmethod
    def _ballot(
        record: EvaluationObservationRecord, actor: str, now: datetime,
    ) -> tuple[list[EvaluationBallot], int]:
        ballots = [EvaluationBallot.model_validate(item) for item in record.ballots]
        index = next((number for number, item in enumerate(ballots) if item.reviewer.casefold() == actor.casefold()), None)
        if index is None:
            if len(ballots) >= 2:
                raise EvaluationConflictError("观察结果已由两位成员认领，可由原复核人修改或重置")
            index = len(ballots)
            ballots.append(EvaluationBallot(reviewer=actor, updated_at=now))
        return ballots, index

    @staticmethod
    def _save_ballots(
        case: EvaluationCaseRecord, record: EvaluationObservationRecord,
        source: EvaluationSource, ballots: list[EvaluationBallot], now: datetime,
    ) -> None:
        references = (
            tuple(ReferenceDefect.model_validate(item) for item in case.reference_defects)
            if case.reference_defects is not None else None
        )
        state, metrics = assessment_metrics(source.findings, tuple(ballots), references)
        record.ballots = [item.model_dump(mode="json") for item in ballots]
        record.assessment_status = state
        record.metrics = metrics
        record.revision += 1
        record.updated_at = now
        # 参考编辑必须感知期间发生的复核，避免旧表单清空新结果。
        case.revision += 1
        case.updated_at = now

    def review_finding(
        self, case_id: str, variant: EvaluationVariant, finding_id: str,
        draft: FindingReviewWrite, actor: str, scope: ResourceScope,
    ) -> ObservationDetail:
        now = datetime.now(UTC)
        with self.sessions() as session:
            case = self._locked_case(session, case_id, scope)
            record = self._observation(session, case_id, variant, scope, lock=True)
            self._revision(record.revision, draft.expected_revision)
            source = EvaluationSource.model_validate(record.source_snapshot)
            if finding_id not in {item.id for item in source.findings}:
                raise EvaluationNotFoundError("评测问题不存在")
            key = draft.decision.reference_key
            if key is not None and (
                draft.decision.verdict.value != "valid"
                or key not in {str(item["key"]) for item in case.reference_defects or []}
            ):
                raise ValueError("只有有效问题可以关联本样本的参考缺陷")
            ballots, index = self._ballot(record, actor, now)
            decisions = {**ballots[index].decisions, finding_id: draft.decision}
            ballots[index] = ballots[index].model_copy(update={
                "decisions": decisions, "submitted_at": None, "updated_at": now,
            })
            self._save_ballots(case, record, source, ballots, now)
            _audit(session, case.dataset_id, actor, "finding_reviewed", {
                "case_id": case_id, "variant": variant, "finding_id": finding_id,
                "verdict": draft.decision.verdict.value, "reference_key": key,
            })
            session.commit()
            return self._observation_detail(record)

    def submit_review(
        self, case_id: str, variant: EvaluationVariant, expected_revision: int,
        actor: str, scope: ResourceScope,
    ) -> ObservationDetail:
        now = datetime.now(UTC)
        with self.sessions() as session:
            case = self._locked_case(session, case_id, scope)
            record = self._observation(session, case_id, variant, scope, lock=True)
            self._revision(record.revision, expected_revision)
            source = EvaluationSource.model_validate(record.source_snapshot)
            ballots, index = self._ballot(record, actor, now)
            if {item.id for item in source.findings} != set(ballots[index].decisions):
                raise ValueError("请先为每条问题保存复核结论，再提交本次复核")
            ballots[index] = ballots[index].model_copy(update={"submitted_at": now, "updated_at": now})
            self._save_ballots(case, record, source, ballots, now)
            _audit(session, case.dataset_id, actor, "review_submitted",
                   {"case_id": case_id, "variant": variant, "revision": record.revision})
            session.commit()
            return self._observation_detail(record)

    def replace_observation(
        self, case_id: str, variant: EvaluationVariant, draft: ObservationReplace,
        actor: str, scope: ResourceScope,
    ) -> ObservationDetail:
        now = datetime.now(UTC)
        with self.sessions() as session:
            source = capture_review_sources(session, (draft.review_run_id,), scope, now)[draft.review_run_id]
            case = self._locked_case(session, case_id, scope)
            dataset = self._dataset(session, case.dataset_id, scope)
            record = self._observation(session, case_id, variant, scope, lock=True)
            self._revision(record.revision, draft.expected_revision)
            if (
                source.repository_id != dataset.repository_id
                or source.installation_id != dataset.installation_id
                or source.repository_key != dataset.repository_key
                or source.pull_request_number != case.pull_request_number
                or source.head_sha != case.head_sha
            ):
                raise ValueError("更换的运行必须属于同一仓库、PR 和提交 SHA")
            if record.source_run_id == source.review_run_id:
                return self._observation_detail(record)
            opposite = session.scalar(select(EvaluationObservationRecord.source_run_id).where(
                EvaluationObservationRecord.case_id == case_id,
                EvaluationObservationRecord.variant != variant,
            ))
            if opposite == source.review_run_id:
                raise EvaluationConflictError("同一条运行不能同时作为基线和候选")
            if record.ballots and not draft.reset_reviews:
                raise EvaluationConflictError("更换运行会清空该组复核，请明确选择重置后保存")
            previous = record.source_run_id
            for key, value in _observation_values(source).items():
                setattr(record, key, value)
            record.revision += 1
            record.updated_at = now
            record.captured_by = actor
            case.revision += 1
            case.updated_at = now
            _audit(session, case.dataset_id, actor, "observation_replaced", {
                "case_id": case_id, "variant": variant, "previous_run_id": previous,
                "source_run_id": source.review_run_id,
            })
            session.commit()
            return self._observation_detail(record)

    @staticmethod
    def _revision(actual: int, expected: int) -> None:
        if actual != expected:
            raise EvaluationConflictError("记录已被更新，请刷新后重试；当前输入可以保留")
