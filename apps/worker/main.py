"""Worker main 职责模块。"""

import logging
import os
import signal

from apps.worker.batches import (
    _dump_truncation_checkpoint as _dump_truncation_checkpoint,
)
from apps.worker.batches import (
    _load_truncation_checkpoint as _load_truncation_checkpoint,
)
from apps.worker.batches import _model_batch_retry_delay as _model_batch_retry_delay
from apps.worker.batches import _model_input_subset as _model_input_subset
from apps.worker.batches import _PersistentBatchedReviewer as _PersistentBatchedReviewer
from apps.worker.batches import (
    _safe_unsupported_parameters_payload as _safe_unsupported_parameters_payload,
)
from apps.worker.batches import _truncation_input_key as _truncation_input_key
from apps.worker.heartbeat import _BusyHeartbeat as _BusyHeartbeat
from apps.worker.heartbeat import _LeaseCursor as _LeaseCursor
from apps.worker.heartbeat import (
    _propagate_task_lease_loss as _propagate_task_lease_loss,
)
from apps.worker.heartbeat import _raise_if_lease_lost as _raise_if_lease_lost
from apps.worker.heartbeat import _record_worker_heartbeat as _record_worker_heartbeat
from apps.worker.heartbeat import _start_worker_heartbeat as _start_worker_heartbeat
from apps.worker.logging_context import LOGGER as LOGGER
from apps.worker.results import _agent_conclusion_payload as _agent_conclusion_payload
from apps.worker.results import _workflow_result as _workflow_result
from apps.worker.runtime import WorkerRuntime
from apps.worker.settings import WorkerSettings
from domain.security import install_redacting_log_filters
from persistence.database import Database
from persistence.operations import SqlAlchemyOperationsRepository
from persistence.retrieval import RetrievalRepository
from persistence.retrieval_runtime import RetrievalRuntimeRepository
from persistence.review_profiles import ReviewProfileRepository
from persistence.task_queue import SqlAlchemyReviewTaskQueue
from services.agent_settings import AgentSettingsService
from services.ai_settings import (
    AiSecretCipher,
    AiSettingsService,
    SqlAlchemyAiRuntimeProvider,
)
from services.evidence_verification import GitHubEvidenceVerifier
from services.github import GitHubApiClient
from services.github_access import GitHubAccessPolicy, with_repository_grants
from services.github_auth import (
    GITHUB_READ_TOKEN_SCOPE,
    GitHubAppSettings,
    GitHubAppTokenProvider,
)
from services.github_code_sources import GitHubCodeSourceLoader
from services.github_context import GitHubReviewContextLoader
from services.github_rules import GitHubRepositoryRuleLoader
from services.operations import OperationsService, OperationsSettings, WorkerMaintenance
from services.rag import ManagedMarkdownKnowledgeBase
from services.retrieval import HybridRetrievalService, RetrievalSettingsService
from services.review_profiles import ReviewProfileRuntimeLoader
from services.telemetry import TelemetryHttpServer


def main() -> None:
    """配置生产 Worker 并启动可响应停止信号的主循环。

    启动步骤：
        1. 配置日志格式和级别；
        2. 从环境读取并校验 Worker 设置；
        3. 创建数据库连接池和持久化队列；
        4. 注册 SIGTERM/SIGINT 处理器；
        5. 运行主循环，并在退出时释放连接池。

    配置或数据库初始化失败会让进程以异常结束，交由 Compose 重启策略处理；
    运行期间的任务级错误由 ``WorkerRuntime`` 按租约规则处理。
    """
    logging.basicConfig(
        level=os.environ.get("OPENREVIEWER_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    install_redacting_log_filters()
    settings = WorkerSettings.from_environment()
    github_api = GitHubApiClient()
    ai_runtime_provider: SqlAlchemyAiRuntimeProvider | None = None
    try:
        database = Database.from_environment()
        github_tokens = GitHubAppTokenProvider(
            github_api,
            GitHubAppSettings.from_environment(),
            GITHUB_READ_TOKEN_SCOPE,
            access_policy=with_repository_grants(GitHubAccessPolicy.from_environment(), database.sessions),
        )
        cipher = AiSecretCipher.from_environment()
        legacy_ai_settings = AiSettingsService(
            database.sessions,
            cipher,
        )
        agent_settings = AgentSettingsService(
            database.sessions,
            cipher,
        )
        ai_runtime_provider = SqlAlchemyAiRuntimeProvider(
            legacy_ai_settings,
            agent_settings,
            max_agent_concurrency=int(
                os.environ.get("OPENREVIEWER_AGENT_MAX_CONCURRENCY", "1")
            ),
        )
        operations_settings = OperationsSettings.from_environment()
        maintenance = WorkerMaintenance(
            OperationsService(
                SqlAlchemyOperationsRepository(database.sessions),
                operations_settings,
            )
        )
    except Exception:
        if ai_runtime_provider is not None:
            ai_runtime_provider.close()
        github_api.close()
        raise
    runtime = WorkerRuntime(
        SqlAlchemyReviewTaskQueue(database.sessions),
        settings,
        context_loader=GitHubReviewContextLoader(github_api, github_tokens),
        rule_loader=GitHubRepositoryRuleLoader(github_api, github_tokens),
        ai_runtime_provider=ai_runtime_provider,
        profile_loader=ReviewProfileRuntimeLoader(
            ReviewProfileRepository(database.sessions),
            cipher,
            max_agent_concurrency=int(
                os.environ.get("OPENREVIEWER_AGENT_MAX_CONCURRENCY", "1")
            ),
        ),
        knowledge_base=ManagedMarkdownKnowledgeBase(
            database.sessions,
            os.environ.get("OPENREVIEWER_KNOWLEDGE_ROOT", "knowledge"),
        ),
        maintenance=maintenance,
        evidence_verifier=GitHubEvidenceVerifier(github_api, github_tokens),
        retrieval_service=HybridRetrievalService(
            RetrievalRepository(database.sessions),
            RetrievalSettingsService(database.sessions, cipher),
            source_loader=GitHubCodeSourceLoader(
                github_api, github_tokens, RetrievalRuntimeRepository(database.sessions)
            ),
        ),
    )

    def stop_worker(_signum: int, _frame: object) -> None:
        """把操作系统停止信号转换为主循环可观察的停止事件。

        参数：
            _signum: 信号编号；当前只需要触发退出，不区分 SIGTERM/SIGINT。
            _frame: Python 信号处理器提供的当前栈帧，同样不参与业务逻辑。

        副作用：
            设置 ``runtime.stop_event``。主循环会在本轮任务边界结束后退出，
            然后写入 ``stopping`` 心跳并释放数据库连接。
        """
        runtime.stop_event.set()

    telemetry_server: TelemetryHttpServer | None = None
    try:
        telemetry_server = TelemetryHttpServer(
            settings.telemetry_host,
            settings.telemetry_port,
        )
        signal.signal(signal.SIGTERM, stop_worker)
        signal.signal(signal.SIGINT, stop_worker)
        telemetry_server.start()
        runtime.run()
    finally:
        if telemetry_server is not None:
            telemetry_server.close()
        ai_runtime_provider.close()
        github_api.close()
        database.dispose()


if __name__ == "__main__":
    main()
