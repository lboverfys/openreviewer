"""PostgreSQL 队列门面：组合租约、上下文、规划、批次和结果存储。"""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.orm import Session, sessionmaker

from domain.enums import ExecutionStatus, FindingOccurrenceStatus, WorkerStatus
from domain.github import GitHubReviewContext
from domain.model_review import MaterializedFinding, ModelReviewInput, ModelReviewResult
from domain.review_planning import RepositoryRulesSnapshot, ReviewPlan
from domain.security import SafeError
from persistence.models import (
    ModelReviewBatchRecord,
    PullRequestVersionRecord,
    ReviewRunRecord,
    ReviewTaskRecord,
)
from persistence.queue import batches as _store_batches
from persistence.queue import budget as _store_budget
from persistence.queue import common as _store_common
from persistence.queue import github as _store_github
from persistence.queue import heartbeat as _store_heartbeat
from persistence.queue import planning as _store_planning
from persistence.queue import progress as _store_progress
from persistence.queue import results as _store_results
from persistence.queue import scheduling as _store_scheduling
from persistence.queue.common import (
    _MAX_TRUNCATION_CHECKPOINT_BYTES as _MAX_TRUNCATION_CHECKPOINT_BYTES,
)
from persistence.queue.common import _as_utc as _as_utc
from persistence.queue.common import _latest_summary_failed as _latest_summary_failed
from persistence.queue.common import (
    _task_run_mutation_load_options as _task_run_mutation_load_options,
)
from persistence.queue.common import (
    _validated_truncation_checkpoint as _validated_truncation_checkpoint,
)
from services.model_budget import ModelBudgetRequest, ModelBudgetReservation
from services.model_review import ModelReviewBatch
from services.task_queue import (
    ModelBatchLease,
    ReviewPlanningInput,
    ReviewTarget,
    ReviewTaskLease,
    StoredModelBatch,
    StoredModelReview,
    StoredReviewPlan,
)


class SqlAlchemyReviewTaskQueue:
    """稳定队列门面；各阶段操作通过共享上下文组合。"""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] | None = None,
        uuid_factory: Callable[[], UUID] | None = None,
        retry_base_seconds: int = 5,
        retry_cap_seconds: int = 300,
        recovery_batch_size: int = 100,
    ) -> None:
        """初始化持久化队列及重试策略。

        队列不依赖内存消息代理，而是直接使用数据库行锁；因此进程重启后任务
        仍然存在。重试延迟默认从 5 秒开始指数增长，最高不超过 300 秒。时钟和
        UUID 工厂可注入，便于测试确定性地模拟过期和检查事件内容。

        参数：
            sessions: 目标数据库的 SQLAlchemy 会话工厂。
            clock: 可选当前时间函数；所有本次操作的时间戳都从它读取。
            uuid_factory: 可选事件 ID 生成器，测试可注入固定 UUID。
            retry_base_seconds: 第一次重试的基础延迟，必须为正数。
            retry_cap_seconds: 指数退避的最大延迟，不能小于基础延迟。

        异常：
            ValueError: 两个退避参数不满足正数/上限关系。

        构造过程不创建数据库事务；会话只在具体队列操作开始时短暂打开。
        """
        if retry_base_seconds <= 0:
            raise ValueError("retry_base_seconds must be positive")
        if retry_cap_seconds < retry_base_seconds:
            raise ValueError("retry_cap_seconds must not be less than the base")
        if not 1 <= recovery_batch_size <= 1000:
            raise ValueError("recovery_batch_size must be between 1 and 1000")
        self._sessions = sessions
        self._clock = clock or (lambda: datetime.now(UTC))
        self._uuid_factory = uuid_factory or uuid4
        self._retry_base_seconds = retry_base_seconds
        self._retry_cap_seconds = retry_cap_seconds
        self._recovery_batch_size = recovery_batch_size

    @staticmethod
    def _validate_heartbeat_identity(worker_id: str, instance_id: str | None) -> None:
        return _store_heartbeat._validate_heartbeat_identity(worker_id, instance_id)

    def start_heartbeat(self, worker_id: str, instance_id: str) -> None:
        return _store_heartbeat.start_heartbeat(self, worker_id, instance_id)

    def record_heartbeat(self, worker_id: str, worker_status: WorkerStatus, current_task_id: str | None=None, *, instance_id: str | None=None) -> None:
        return _store_heartbeat.record_heartbeat(self, worker_id, worker_status, current_task_id, instance_id=instance_id)

    def recover_expired_leases(self) -> int:
        return _store_scheduling.recover_expired_leases(self)

    def claim_next(self, worker_id: str, lease_duration: timedelta, *, ai_configured: bool=True) -> ReviewTaskLease | None:
        return _store_scheduling.claim_next(self, worker_id, lease_duration, ai_configured=ai_configured)

    def renew_lease(self, lease: ReviewTaskLease, lease_duration: timedelta) -> ReviewTaskLease:
        return _store_scheduling.renew_lease(self, lease, lease_duration)

    def load_target(self, lease: ReviewTaskLease) -> ReviewTarget:
        return _store_github.load_target(self, lease)

    def store_github_context(self, lease: ReviewTaskLease, context: GitHubReviewContext, *, ci_poll_interval: timedelta, ci_wait_timeout: timedelta) -> ExecutionStatus:
        return _store_github.store_github_context(self, lease, context, ci_poll_interval=ci_poll_interval, ci_wait_timeout=ci_wait_timeout)

    def load_planning_input(self, lease: ReviewTaskLease) -> ReviewPlanningInput:
        return _store_planning.load_planning_input(self, lease)

    def store_review_plan(self, lease: ReviewTaskLease, rules: RepositoryRulesSnapshot, plan: ReviewPlan) -> StoredReviewPlan:
        return _store_planning.store_review_plan(self, lease, rules, plan)

    def load_model_review_input(self, lease: ReviewTaskLease) -> ModelReviewInput:
        return _store_planning.load_model_review_input(self, lease)

    @staticmethod
    def _reconcile_finding_lifecycles(session: Session, run: ReviewRunRecord, findings: tuple[MaterializedFinding, ...], now: datetime, *, coverage_complete: bool) -> tuple[dict[str, tuple[FindingOccurrenceStatus, int, str | None]], int]:
        return _store_results._reconcile_finding_lifecycles(session, run, findings, now, coverage_complete=coverage_complete)

    def store_model_review(self, lease: ReviewTaskLease, review_input: ModelReviewInput, result: ModelReviewResult, findings: tuple[MaterializedFinding, ...], *, configuration_revision: int | None=None, partial: bool=False) -> StoredModelReview:
        return _store_results.store_model_review(self, lease, review_input, result, findings, configuration_revision=configuration_revision, partial=partial)

    def record_model_progress(self, lease: ReviewTaskLease, phase: str, payload: Mapping[str, object], *, agent: str='default') -> None:
        return _store_progress.record_model_progress(self, lease, phase, payload, agent=agent)

    def mark_model_aggregating(self, lease: ReviewTaskLease) -> None:
        return _store_progress.mark_model_aggregating(self, lease)

    def ensure_model_batches(self, lease: ReviewTaskLease, batches: tuple[ModelReviewBatch, ...], *, agent: str='default') -> tuple[StoredModelBatch, ...]:
        return _store_batches.ensure_model_batches(self, lease, batches, agent=agent)

    def claim_model_batch(self, lease: ReviewTaskLease, batch_number: int, *, agent: str='default', lease_duration: timedelta) -> StoredModelBatch:
        return _store_batches.claim_model_batch(self, lease, batch_number, agent=agent, lease_duration=lease_duration)

    def renew_model_batch(self, lease: ReviewTaskLease, batch_number: int, *, agent: str='default', lease_duration: timedelta) -> StoredModelBatch:
        return _store_batches.renew_model_batch(self, lease, batch_number, agent=agent, lease_duration=lease_duration)

    def renew_model_batches(self, lease: ReviewTaskLease, batches: tuple[ModelBatchLease, ...]) -> tuple[StoredModelBatch, ...]:
        return _store_batches.renew_model_batches(self, lease, batches)

    def complete_model_batch(self, lease: ReviewTaskLease, batch_number: int, result: ModelReviewResult, *, agent: str='default', expected_attempt_count: int | None=None) -> StoredModelBatch:
        return _store_batches.complete_model_batch(self, lease, batch_number, result, agent=agent, expected_attempt_count=expected_attempt_count)

    def checkpoint_model_batch(self, lease: ReviewTaskLease, batch_number: int, checkpoint: Mapping[str, object], *, agent: str='default', expected_attempt_count: int | None=None) -> StoredModelBatch:
        return _store_batches.checkpoint_model_batch(self, lease, batch_number, checkpoint, agent=agent, expected_attempt_count=expected_attempt_count)

    def fail_model_batch(self, lease: ReviewTaskLease, batch_number: int, error: SafeError, *, agent: str='default', retry_delay: timedelta | None=None, expected_attempt_count: int | None=None) -> StoredModelBatch:
        return _store_batches.fail_model_batch(self, lease, batch_number, error, agent=agent, retry_delay=retry_delay, expected_attempt_count=expected_attempt_count)

    def load_model_batches(self, lease: ReviewTaskLease, *, agent: str='default') -> tuple[StoredModelBatch, ...]:
        return _store_batches.load_model_batches(self, lease, agent=agent)

    @staticmethod
    def _stored_model_batch(row: ModelReviewBatchRecord) -> StoredModelBatch:
        return _store_batches._stored_model_batch(row)

    def reserve_repository_request(self, lease: ReviewTaskLease, limit: int) -> None:
        return _store_budget.reserve_repository_request(self, lease, limit)

    def monthly_accountant(self, lease: Callable[[], ReviewTaskLease], agent: str):
        return _store_budget.monthly_accountant(self, lease, agent)

    def model_egress_context(self, repository: str, run_id: str):
        from persistence.egress import repository_egress
        return repository_egress(self._sessions, repository, run_id)

    def load_reused_review(self, lease: ReviewTaskLease, key: str):
        from persistence.review_reuse import load_reused_review
        return load_reused_review(self, lease, key)

    def store_reusable_review(self, lease: ReviewTaskLease, key: str, head_sha: str, payload):
        from persistence.review_reuse import store_reusable_review
        return store_reusable_review(self, lease, key, head_sha, payload)

    def task_profile_id(self, lease: ReviewTaskLease) -> str | None:
        return _store_budget.task_profile_id(self, lease)

    def pause_for_monthly_budget(self, lease: ReviewTaskLease, error: SafeError) -> None:
        return _store_budget.pause_for_monthly_budget(self, lease, error)

    def reserve_model_budget(self, lease: ReviewTaskLease, request: ModelBudgetRequest, *, agent: str='default') -> ModelBudgetReservation:
        return _store_budget.reserve_model_budget(self, lease, request, agent=agent)

    def settle_model_budget(self, reservation: ModelBudgetReservation, *, input_tokens: int | None, output_tokens: int | None, estimated_cost_microusd: int | None, response_status: int | None, duration_ms: int, uncertain: bool=False) -> None:
        return _store_budget.settle_model_budget(self, reservation, input_tokens=input_tokens, output_tokens=output_tokens, estimated_cost_microusd=estimated_cost_microusd, response_status=response_status, duration_ms=duration_ms, uncertain=uncertain)

    def pause_for_model_budget(self, lease: ReviewTaskLease, error: SafeError) -> None:
        return _store_budget.pause_for_model_budget(self, lease, error)

    def mark_waiting_for_ci(self, lease: ReviewTaskLease) -> None:
        return _store_scheduling.mark_waiting_for_ci(self, lease)

    def retry_or_fail(self, lease: ReviewTaskLease, error: SafeError) -> None:
        return _store_scheduling.retry_or_fail(self, lease, error)

    def heartbeat_is_fresh(self, worker_id: str, max_age: timedelta) -> bool:
        return _store_heartbeat.heartbeat_is_fresh(self, worker_id, max_age)

    @staticmethod
    def _locked_owned_task(session: Session, lease: ReviewTaskLease, now: datetime) -> ReviewTaskRecord:
        return _store_common._locked_owned_task(session, lease, now)

    @staticmethod
    def _locked_owned_task_with_run(session: Session, lease: ReviewTaskLease, now: datetime, *, include_repository_policy: bool=False) -> tuple[ReviewTaskRecord, ReviewRunRecord]:
        return _store_common._locked_owned_task_with_run(session, lease, now, include_repository_policy=include_repository_policy)

    def _get_or_create_version(self, session: Session, run: ReviewRunRecord, now: datetime) -> PullRequestVersionRecord:
        return _store_github._get_or_create_version(self, session, run, now)

    @staticmethod
    def _update_pull_request_snapshot(version: PullRequestVersionRecord, context: GitHubReviewContext, now: datetime) -> None:
        return _store_github._update_pull_request_snapshot(version, context, now)

    @staticmethod
    def _replace_files(session: Session, version_id: str, context: GitHubReviewContext, now: datetime) -> None:
        return _store_github._replace_files(session, version_id, context, now)

    @staticmethod
    def _replace_ci_checks(session: Session, version_id: str, context: GitHubReviewContext, now: datetime) -> None:
        return _store_github._replace_ci_checks(session, version_id, context, now)

    @staticmethod
    def _supersede_previous_versions(session: Session, current_run: ReviewRunRecord, now: datetime) -> int:
        return _store_github._supersede_previous_versions(session, current_run, now)

    @staticmethod
    def _set_owned_status(task: ReviewTaskRecord, run: ReviewRunRecord, status: ExecutionStatus, now: datetime) -> None:
        return _store_common._set_owned_status(task, run, status, now)

    @staticmethod
    def _set_workflow_status(task: ReviewTaskRecord, run: ReviewRunRecord, status: ExecutionStatus, now: datetime) -> None:
        return _store_common._set_workflow_status(task, run, status, now)

    def _reschedule_or_fail(self, session: Session, task: ReviewTaskRecord, run: ReviewRunRecord, now: datetime, error: SafeError, *, event_suffix: str) -> None:
        return _store_common._reschedule_or_fail(self, session, task, run, now, error, event_suffix=event_suffix)

    def _retry_delay(self, attempt_count: int) -> timedelta:
        return _store_common._retry_delay(self, attempt_count)

    @staticmethod
    def _batch_retry_at(error: SafeError, now: datetime) -> datetime | None:
        return _store_common._batch_retry_at(error, now)

    def _add_event(self, session: Session, task: ReviewTaskRecord | None, event_type: str, key_suffix: str, occurred_at: datetime, *, error: SafeError | None=None, extra_payload: Mapping[str, object] | None=None, aggregate_id: str | None=None) -> None:
        return _store_common._add_event(self, session, task, event_type, key_suffix, occurred_at, error=error, extra_payload=extra_payload, aggregate_id=aggregate_id)
