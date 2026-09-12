"""OpenReviewer 的 FastAPI 入口。"""

import ipaddress
import os
import time
from collections.abc import Mapping
from contextlib import asynccontextmanager
from threading import RLock
from typing import Annotated
from urllib.parse import urlsplit, urlunsplit

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
    Response,
    status,
)
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.concurrency import run_in_threadpool

from apps.api.routes.dashboard import register_dashboard_routes
from apps.api.routes.evaluations import register_evaluation_routes
from apps.api.routes.knowledge import register_knowledge_routes
from apps.api.routes.retrieval import register_retrieval_routes
from apps.api.routes.reviews import register_review_routes
from apps.api.routes.settings import register_settings_routes
from apps.api.routes.team import register_team_routes
from apps.api.schemas import (
    AiSettingsResponse,
    AuthResponse,
    DashboardResponse,
    HealthResponse,
    LoginRequest,
    ReadinessResponse,
    WebhookReceiptResponse,
)
from domain.enums import ExecutionStatus
from domain.security import (
    ErrorCode,
    SafeApplicationError,
    SafeError,
    install_redacting_log_filters,
)
from persistence.auth import SqlAlchemyLoginAttemptLimiter, SqlAlchemySessionStore
from persistence.dashboard import SqlAlchemyDashboardRepository
from persistence.database import Database, DatabaseConfigurationError
from persistence.external_actions import SqlAlchemyExternalActionStore
from persistence.operations import SqlAlchemyOperationsRepository
from persistence.repositories import SqlAlchemyReviewRepository
from persistence.retrieval import RetrievalRepository
from persistence.review_management import SqlAlchemyReviewManagementRepository
from persistence.team import SqlAlchemyMemberStore
from persistence.webhooks import SqlAlchemyGitHubWebhookRepository
from services.agent_settings import AgentSettingsService
from services.ai_settings import (
    AiProviderNotReadyError,
    AiSecretCipher,
    AiSettingsConfigurationError,
    AiSettingsConflictError,
    AiSettingsPersistenceError,
    AiSettingsService,
    AiSettingsValidationError,
)
from services.auth import (
    AuthConfigurationError,
    AuthPersistenceError,
    AuthService,
    AuthSettings,
    InvalidSessionError,
    LoginAttemptLimiter,
    LoginLimiter,
    LoginRateLimitError,
    SessionPrincipal,
)
from services.dashboard import (
    DashboardPersistenceError,
    DashboardService,
)
from services.dashboard_stream import (
    DashboardStreamCoordinator,
    DashboardStreamRegistry,
)
from services.evaluation_workbench import EvaluationWorkbench
from services.github import GitHubApiClient
from services.github_access import (
    GitHubAccessConfigurationError,
    GitHubAccessPolicy,
)
from services.github_auth import (
    GITHUB_PUBLISH_TOKEN_SCOPE,
    GITHUB_READ_TOKEN_SCOPE,
    GitHubAppSettings,
    GitHubAppTokenProvider,
)
from services.github_context import GitHubReviewContextLoader
from services.github_publisher import GitHubReviewPublisher
from services.operations import (
    OperationsError,
    OperationsService,
    OperationsSettings,
    ReadinessSnapshot,
)
from services.rag import (
    KnowledgeConflictError,
    KnowledgeNotFoundError,
    KnowledgePersistenceError,
    KnowledgeValidationError,
    ManagedMarkdownKnowledgeBase,
    MarkdownKnowledgeBase,
)
from services.rbac import Permission, ResourceScope, has_permission, permissions_for
from services.retrieval import HybridRetrievalService, RetrievalSettingsService
from services.review_management import (
    PullRequestIdentityLoader,
    ReviewManagementService,
)
from services.reviews import (
    ReviewService,
)
from services.team import TeamService
from services.telemetry import GLOBAL_TELEMETRY, TelemetryRegistry
from services.webhooks import (
    GitHubWebhookService,
    GitHubWebhookSettings,
    WebhookConfigurationError,
    WebhookRequestError,
)


def _trusted_proxy_networks(
    values: Mapping[str, str] | None = None,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """读取可信反向代理网段；无法解析时失败关闭。"""

    source = values if values is not None else os.environ
    raw = source.get(
        "OPENREVIEWER_TRUSTED_PROXY_CIDRS",
        "127.0.0.1/32,::1/128,172.23.0.0/16",
    )
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for item in raw.split(","):
        value = item.strip()
        if not value:
            continue
        try:
            networks.append(ipaddress.ip_network(value, strict=False))
        except ValueError as exc:
            raise ValueError("OPENREVIEWER_TRUSTED_PROXY_CIDRS contains invalid CIDR") from exc
    return tuple(networks)


def _require_origin_header(values: Mapping[str, str] | None = None) -> bool:
    """读取是否拒绝缺少 Origin/Referer 的副作用请求。"""

    source = values if values is not None else os.environ
    return source.get("OPENREVIEWER_REQUIRE_ORIGIN", "false").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _request_client_is_trusted_proxy(
    request: Request,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    raw_address = request.client.host if request.client is not None else None
    if not raw_address:
        return False
    try:
        address = ipaddress.ip_address(raw_address)
    except ValueError:
        return False
    return any(address in network for network in networks)


def _normalized_authority(scheme: str, hostname: str, port: int | None) -> str:
    normalized_scheme = scheme.casefold()
    normalized_host = hostname.casefold().rstrip(".")
    default_port = 443 if normalized_scheme == "https" else 80
    suffix = "" if port in (None, default_port) else f":{port}"
    return f"{normalized_scheme}://{normalized_host}{suffix}"


def create_app(
    review_service: ReviewService | None = None,
    auth_service: AuthService | None = None,
    dashboard_service: DashboardService | None = None,
    login_limiter: LoginLimiter | None = None,
    webhook_service: GitHubWebhookService | None = None,
    ai_settings_service: AiSettingsService | None = None,
    agent_settings_service: AgentSettingsService | None = None,
    knowledge_base: MarkdownKnowledgeBase | None = None,
    review_management_service: ReviewManagementService | None = None,
    identity_loader: PullRequestIdentityLoader | None = None,
    operations_service: OperationsService | None = None,
    dashboard_stream: DashboardStreamCoordinator[DashboardResponse] | None = None,
    github_access_policy: GitHubAccessPolicy | None = None,
    telemetry_registry: TelemetryRegistry | None = None,
    retrieval_service: HybridRetrievalService | None = None,
    team_service: TeamService | None = None,
    evaluation_service: EvaluationWorkbench | None = None,
) -> FastAPI:
    """创建带依赖注入边界的 FastAPI 应用实例。

    参数：
        review_service: 可选审查提交服务；不传时在第一次创建任务时懒加载数据库
            仓储。测试通常传入 SQLite 仓储，避免依赖真实 PostgreSQL。
        auth_service: 可选认证服务；不传时在第一次需要认证的请求时读取环境配置。
        dashboard_service: 可选 Dashboard 查询服务；不传时按需创建数据库适配器。
        login_limiter: 可选登录失败限流器；不传时创建当前 API 进程专用实例。

    返回：
        已注册健康检查、认证、Dashboard、SSE 和任务创建路由的 ``FastAPI`` 应用。

    依赖初始化采用懒加载：只访问 ``/healthz`` 不会触发数据库或认证配置读取；
    应用关闭时只释放本函数自己创建的数据库，不会销毁调用方注入的测试资源。
    各路由内部把配置/持久化异常转换成稳定 HTTP 状态，避免泄露底层凭据。
    """

    telemetry = telemetry_registry or GLOBAL_TELEMETRY
    trusted_proxy_networks = _trusted_proxy_networks()
    require_origin_header = _require_origin_header()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        """管理 FastAPI 应用生命周期。

        数据库采用延迟初始化，只有真正访问需要持久化的接口时才建立连接。应用
        关闭时从 ``state`` 取出由本实例拥有的数据库并释放连接池，注入的测试
        服务不会被错误地销毁。

        参数：
            application: FastAPI 传入的当前应用对象，用于读取本实例的状态容器。

        生命周期：
            进入时不主动连接数据库；路由完成后先让应用退出，再释放懒加载的
            ``owned_database``。异常不会吞掉，仍交由 ASGI 服务器报告。
        """
        install_redacting_log_filters()
        yield
        database: Database | None = application.state.owned_database
        github_api: GitHubApiClient | None = application.state.owned_github_api
        if github_api is not None:
            github_api.close()
        if database is not None:
            database.dispose()

    application = FastAPI(
        title="OpenReviewer API",
        version="0.2.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    application.state.retrieval_service = retrieval_service
    application.state.review_service = review_service
    application.state.auth_service = auth_service
    application.state.dashboard_service = dashboard_service
    application.state.webhook_service = webhook_service
    application.state.ai_settings_service = ai_settings_service
    application.state.agent_settings_service = agent_settings_service
    application.state.knowledge_base = knowledge_base
    application.state.review_management_service = review_management_service
    application.state.identity_loader = identity_loader
    application.state.operations_service = operations_service
    application.state.dashboard_stream = dashboard_stream
    # 受限账号必须各自拥有独立的 SSE 快照缓存；注册表有界，避免登录范围
    # 持续变化时把协调器和最近快照永久留在 API 进程内存中。
    application.state.dashboard_streams = DashboardStreamRegistry[DashboardResponse]()
    application.state.github_access_policy = github_access_policy
    application.state.login_limiter = login_limiter or (
        LoginAttemptLimiter() if auth_service is not None else None
    )
    application.state.owned_database = None
    application.state.owned_github_api = None

    @application.middleware("http")
    async def record_request_duration(request: Request, call_next):
        """按路由模板记录请求耗时，避免主键和查询串进入指标标签。"""

        started = time.monotonic()
        response_status = status.HTTP_500_INTERNAL_SERVER_ERROR
        try:
            response = await call_next(request)
            response_status = response.status_code
            return response
        finally:
            route = request.scope.get("route")
            route_template = getattr(route, "path", "unmatched")
            telemetry.observe_http(
                request.method,
                route_template,
                response_status,
                time.monotonic() - started,
            )

    initialization_lock = RLock()

    def get_database() -> Database:
        """懒加载并缓存数据库连接。

        使用进程内锁避免并发请求同时创建多个引擎；配置缺失时转换为 503，让
        ``/healthz`` 仍可用于存活检查，同时不泄露数据库凭据或具体配置细节。

        返回：
            当前应用缓存的 ``Database`` 实例；第一次调用成功后复用同一连接池。

        异常：
            HTTPException(503): 数据库配置缺失或无效。底层配置异常被保留为原因，
            但响应只返回稳定的“持久化未配置”提示。

        进程内锁只防止同一 API 副本重复初始化，不能替代数据库连接池或跨进程锁。
        """
        configured_database: Database | None = application.state.owned_database
        if configured_database is not None:
            return configured_database
        with initialization_lock:
            configured_database = application.state.owned_database
            if configured_database is not None:
                return configured_database
            try:
                configured_database = Database.from_environment()
            except DatabaseConfigurationError as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="review persistence is not configured",
                ) from exc
            application.state.owned_database = configured_database
            return configured_database


    def get_retrieval_service() -> HybridRetrievalService:
        configured: HybridRetrievalService | None = application.state.retrieval_service
        if configured is not None:
            return configured
        with initialization_lock:
            configured = application.state.retrieval_service
            if configured is None:
                try:
                    sessions = get_database().sessions
                    configured = HybridRetrievalService(
                        RetrievalRepository(sessions),
                        RetrievalSettingsService(sessions, AiSecretCipher.from_environment()),
                    )
                except AiSettingsConfigurationError as exc:
                    raise HTTPException(503, "检索加密配置暂时不可用") from exc
                application.state.retrieval_service = configured
        return configured

    def get_operations_service() -> OperationsService:
        """返回共享数据库上的就绪、指标和维护服务。"""

        configured_service: OperationsService | None = (
            application.state.operations_service
        )
        if configured_service is not None:
            return configured_service
        with initialization_lock:
            configured_service = application.state.operations_service
            if configured_service is None:
                configured_service = OperationsService(
                    SqlAlchemyOperationsRepository(get_database().sessions),
                    OperationsSettings.from_environment(),
                )
                application.state.operations_service = configured_service
            return configured_service

    def get_review_service() -> ReviewService:
        """返回注入的或按需构造的审查提交服务。

        返回：
            优先返回 ``create_app`` 调用方注入的服务；否则用懒加载数据库会话工厂
            创建 ``SqlAlchemyReviewRepository`` 和 ``ReviewService``，并缓存到应用状态。

        该函数不提交任务；真正的请求校验、指纹计算和事务写入发生在 POST 路由
        调用服务的 ``submit`` 方法时。初始化数据库失败会通过 ``get_database``
        转换为 503。
        """
        configured_service: ReviewService | None = application.state.review_service
        if configured_service is not None:
            return configured_service
        with initialization_lock:
            configured_service = application.state.review_service
            if configured_service is None:
                configured_service = ReviewService(
                    SqlAlchemyReviewRepository(get_database().sessions)
                )
                application.state.review_service = configured_service
            return configured_service

    def get_auth_service() -> AuthService:
        """返回认证服务，并在首次使用时校验所有安全配置。

        返回：
            注入的认证服务，或根据环境变量创建并缓存的 ``AuthService``。

        异常：
            HTTPException(503): 管理员用户名、Argon2id 哈希、会话密钥或 TTL 配置
            缺失/非法。具体配置错误不会直接返回给客户端。

        健康检查不依赖该函数，因此认证配置故障不会阻止容器报告进程存活。
        """
        configured_service: AuthService | None = application.state.auth_service
        if configured_service is not None:
            return configured_service
        with initialization_lock:
            configured_service = application.state.auth_service
            if configured_service is None:
                try:
                    settings = AuthSettings.from_environment()
                    member_store = SqlAlchemyMemberStore(get_database().sessions)
                    member_store.bootstrap(settings)
                    configured_service = AuthService(
                        settings,
                        session_store=SqlAlchemySessionStore(get_database().sessions),
                        member_store=member_store,
                    )
                except (AuthConfigurationError, AuthPersistenceError, HTTPException) as exc:
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="administrator authentication is not configured",
                    ) from exc
                application.state.auth_service = configured_service
            return configured_service

    def get_team_service() -> TeamService:
        if team_service is not None:
            return team_service
        return TeamService(
            get_database().sessions, get_auth_service().settings.username,
        )

    def get_evaluation_service() -> EvaluationWorkbench:
        return evaluation_service or EvaluationWorkbench(get_database().sessions)

    def get_login_limiter() -> LoginLimiter:
        """返回注入的限流器，或懒加载数据库共享实现。"""

        configured_limiter: LoginLimiter | None = application.state.login_limiter
        if configured_limiter is not None:
            return configured_limiter
        with initialization_lock:
            configured_limiter = application.state.login_limiter
            if configured_limiter is None:
                configured_limiter = SqlAlchemyLoginAttemptLimiter(
                    get_database().sessions
                )
                application.state.login_limiter = configured_limiter
            return configured_limiter

    def get_dashboard_service() -> DashboardService:
        """返回注入的或按需构造的 Dashboard 查询服务。

        返回：
            注入的服务，或使用懒加载数据库会话工厂创建并缓存的
            ``DashboardService``。

        该函数只准备查询边界，不执行 Dashboard 查询；数据库读取错误由调用方
        ``dashboard_snapshot`` 统一转换为 503。
        """
        configured_service: DashboardService | None = (
            application.state.dashboard_service
        )
        if configured_service is not None:
            return configured_service
        with initialization_lock:
            configured_service = application.state.dashboard_service
            if configured_service is None:
                configured_service = DashboardService(
                    SqlAlchemyDashboardRepository(get_database().sessions)
                )
                application.state.dashboard_service = configured_service
            return configured_service

    def get_webhook_service() -> GitHubWebhookService:
        """返回注入的服务，或按需配置验签接入服务。"""

        configured_service: GitHubWebhookService | None = (
            application.state.webhook_service
        )
        if configured_service is not None:
            return configured_service
        with initialization_lock:
            configured_service = application.state.webhook_service
            if configured_service is None:
                try:
                    settings = GitHubWebhookSettings.from_environment()
                    access_policy = get_github_access_policy()
                    database = get_database()
                except (
                    WebhookConfigurationError,
                    GitHubAccessConfigurationError,
                    HTTPException,
                ) as exc:
                    raise SafeApplicationError(
                        SafeError(
                            code=ErrorCode.WEBHOOK_NOT_CONFIGURED,
                            safe_message="GitHub Webhook 接入尚未完成配置",
                            retryable=True,
                        )
                    ) from exc
                configured_service = GitHubWebhookService(
                    SqlAlchemyGitHubWebhookRepository(database.sessions),
                    settings,
                    access_policy,
                )
                application.state.webhook_service = configured_service
            return configured_service

    def get_github_access_policy() -> GitHubAccessPolicy:
        """返回共享 GitHub 接入白名单，配置缺失时保持失败关闭。"""

        configured_policy: GitHubAccessPolicy | None = (
            application.state.github_access_policy
        )
        if configured_policy is not None:
            return configured_policy
        with initialization_lock:
            configured_policy = application.state.github_access_policy
            if configured_policy is None:
                try:
                    configured_policy = GitHubAccessPolicy.from_environment()
                except GitHubAccessConfigurationError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="GitHub access policy is not configured",
                    ) from exc
                application.state.github_access_policy = configured_policy
            return configured_policy

    def get_ai_settings_service() -> AiSettingsService:
        """返回注入的或按需创建的动态 AI 配置服务。"""

        configured_service: AiSettingsService | None = (
            application.state.ai_settings_service
        )
        if configured_service is not None:
            return configured_service
        with initialization_lock:
            configured_service = application.state.ai_settings_service
            if configured_service is None:
                try:
                    cipher = AiSecretCipher.from_environment()
                    database = get_database()
                except AiSettingsConfigurationError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="AI settings encryption is not configured",
                    ) from exc
                configured_service = AiSettingsService(database.sessions, cipher)
                application.state.ai_settings_service = configured_service
            return configured_service

    def get_agent_settings_service() -> AgentSettingsService:
        """返回固定 DAG 的独立 Agent 配置服务。"""

        configured_service: AgentSettingsService | None = (
            application.state.agent_settings_service
        )
        if configured_service is not None:
            return configured_service
        with initialization_lock:
            configured_service = application.state.agent_settings_service
            if configured_service is None:
                try:
                    cipher = AiSecretCipher.from_environment()
                    database = get_database()
                except AiSettingsConfigurationError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="AI settings encryption is not configured",
                    ) from exc
                configured_service = AgentSettingsService(database.sessions, cipher)
                application.state.agent_settings_service = configured_service
            return configured_service

    def get_knowledge_base() -> MarkdownKnowledgeBase:
        configured = application.state.knowledge_base
        if configured is None:
            with initialization_lock:
                configured = application.state.knowledge_base
                if configured is None:
                    configured = ManagedMarkdownKnowledgeBase(
                        get_database().sessions,
                        os.environ.get("OPENREVIEWER_KNOWLEDGE_ROOT", "knowledge"),
                    )
                    application.state.knowledge_base = configured
        return configured

    def get_managed_knowledge_base() -> ManagedMarkdownKnowledgeBase:
        configured = get_knowledge_base()
        if not isinstance(configured, ManagedMarkdownKnowledgeBase):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="knowledge management is not configured",
            )
        return configured

    def get_review_management_service() -> ReviewManagementService:
        """返回任务详情与人工控制服务。"""

        configured_service: ReviewManagementService | None = (
            application.state.review_management_service
        )
        if configured_service is not None:
            return configured_service
        with initialization_lock:
            configured_service = application.state.review_management_service
            if configured_service is None:
                publisher = None
                identity_loader = application.state.identity_loader
                # 详情读取不依赖 GitHub 凭据。只有 App ID 和私钥路径都存在时
                # 才装配人工发布器；配置缺失会在用户点击发布时准确返回 503，
                # 配置存在但无效则同样不会伪造发布成功。
                if (
                    os.environ.get("OPENREVIEWER_GITHUB_APP_ID", "").strip()
                    and os.environ.get(
                        "OPENREVIEWER_GITHUB_PRIVATE_KEY_FILE",
                        "",
                    ).strip()
                ):
                    github_api: GitHubApiClient | None = None
                    try:
                        github_api = GitHubApiClient()
                        github_read_tokens = GitHubAppTokenProvider(
                            github_api,
                            GitHubAppSettings.from_environment(),
                            GITHUB_READ_TOKEN_SCOPE,
                            access_policy=get_github_access_policy(),
                        )
                        github_publish_tokens = GitHubAppTokenProvider(
                            github_api,
                            GitHubAppSettings.from_environment(),
                            GITHUB_PUBLISH_TOKEN_SCOPE,
                            access_policy=get_github_access_policy(),
                        )
                        configured_identity_loader = GitHubReviewContextLoader(
                            github_api,
                            github_read_tokens,
                        )
                        configured_publisher = GitHubReviewPublisher(
                            github_api,
                            github_publish_tokens,
                            action_store=SqlAlchemyExternalActionStore(
                                get_database().sessions
                            ),
                        )
                        identity_loader = configured_identity_loader
                        publisher = configured_publisher
                        application.state.owned_github_api = github_api
                    except (ValueError, OSError):
                        if github_api is not None:
                            github_api.close()
                configured_service = ReviewManagementService(
                    SqlAlchemyReviewManagementRepository(
                        get_database().sessions,
                        publisher=publisher,
                    ),
                    identity_loader=identity_loader,
                )
                application.state.review_management_service = configured_service
            return configured_service

    def ai_settings_response() -> AiSettingsResponse:
        try:
            return AiSettingsResponse.from_view(get_ai_settings_service().get())
        except AiSettingsPersistenceError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="AI settings are temporarily unavailable",
            ) from exc

    def translate_ai_settings_error(exc: Exception) -> HTTPException:
        """把配置领域错误转换成稳定且不含敏感信息的 HTTP 错误。"""

        if isinstance(exc, AiSettingsConflictError):
            return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
        if isinstance(exc, (AiSettingsValidationError, AiProviderNotReadyError)):
            return HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            )
        if isinstance(exc, AiSettingsPersistenceError):
            return HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="AI settings are temporarily unavailable",
            )
        if isinstance(exc, AiSettingsConfigurationError):
            return HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="AI settings encryption is not configured",
            )
        return HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="AI settings operation failed",
        )

    def translate_knowledge_error(exc: Exception) -> HTTPException:
        if isinstance(exc, KnowledgeNotFoundError):
            return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
        if isinstance(exc, KnowledgeConflictError):
            return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
        if isinstance(exc, KnowledgeValidationError):
            return HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            )
        if isinstance(exc, KnowledgePersistenceError):
            return HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="knowledge base is temporarily unavailable",
            )
        return HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="knowledge operation failed",
        )

    def require_principal(request: Request) -> SessionPrincipal:
        """从请求 Cookie 验证当前管理员身份。

        参数：
            request: FastAPI 当前 HTTP 请求，用于读取认证服务决定的 Cookie 名称。

        返回：
            经过 HMAC、主体和有效期校验的 ``SessionPrincipal``，供路由作为认证
            依赖使用。

        异常：
            HTTPException(401): Cookie 缺失、签名错误、主体不匹配或会话已过期；
            响应同时禁止缓存并带 ``WWW-Authenticate: Session``。
            HTTPException(503): 认证服务配置无法加载。
        """
        service = get_auth_service()
        try:
            return service.verify_session(
                request.cookies.get(service.settings.cookie_name)
            )
        except InvalidSessionError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="authentication required",
                headers={
                    "WWW-Authenticate": "Session",
                    "Cache-Control": "no-store",
                },
            ) from exc
        except AuthPersistenceError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="authentication state is temporarily unavailable",
                headers={"Cache-Control": "no-store"},
            ) from exc

    def require_permission(permission: Permission):
        """创建返回会话主体的权限依赖，认证成功但越权时统一返回 403。"""

        def dependency(
            principal: Annotated[SessionPrincipal, Depends(require_principal)],
        ) -> SessionPrincipal:
            if not has_permission(principal.role, permission):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="permission denied",
                )
            return principal

        return dependency

    require_review_viewer = require_permission(Permission.VIEW_REVIEWS)
    require_adjudicator = require_permission(Permission.ADJUDICATE_FINDINGS)
    require_review_manager = require_permission(Permission.MANAGE_REVIEWS)
    require_settings_manager = require_permission(Permission.MANAGE_SETTINGS)
    require_knowledge_manager = require_permission(Permission.MANAGE_KNOWLEDGE)

    def ensure_permission(
        principal: SessionPrincipal,
        permission: Permission,
    ) -> None:
        if not has_permission(principal.role, permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="permission denied",
            )

    def require_same_origin(request: Request) -> None:
        """校验带副作用请求的 Origin/Referer 与当前代理入口一致。

        生产配置要求请求至少携带 Origin 或同源 Referer；本地兼容模式允许无头
        的非浏览器调用。有 Origin 时优先使用反向代理传入的协议，并只比较 scheme
        和 host，防止跨站页面借用管理员 Cookie 发起登录、登出或创建任务请求。

        参数：
            request: 要检查的请求。只读取 ``Origin``、``Referer``、``Host`` 和可选的
                ``X-Forwarded-Proto`` 请求头。

        返回：
            校验通过时返回 ``None``。

        异常：
            HTTPException(403): 同源头缺失或 scheme/host 与当前代理入口不一致。

        该依赖不验证登录身份，也不检查 CSRF Token；身份验证由
        ``require_principal`` 单独负责。是否拒绝缺少同源头由
        ``OPENREVIEWER_REQUIRE_ORIGIN`` 控制。
        """
        origin = request.headers.get("origin")
        if not origin:
            if not require_origin_header:
                return
            referer = request.headers.get("referer")
            if not referer:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="same-origin header required",
                )
            # Referer 允许携带路径；先只保留其 scheme/authority，再复用下面
            # 对 Origin 的严格凭据、端口和字符校验。
            try:
                parsed_referer = urlsplit(referer)
                origin = urlunsplit(
                    (parsed_referer.scheme, parsed_referer.netloc, "", "", "")
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="cross-origin request rejected",
                ) from exc
        trusted_proxy = _request_client_is_trusted_proxy(
            request,
            trusted_proxy_networks,
        )
        parsed_authority = None
        try:
            if trusted_proxy:
                forwarded_proto = request.headers.get("x-forwarded-proto")
                forwarded_host = request.headers.get("x-forwarded-host")
                scheme = (
                    forwarded_proto.split(",", 1)[0].strip().casefold()
                    if forwarded_proto
                    else request.url.scheme
                )
                authority = forwarded_host or request.headers.get("host", "")
                parsed_authority = urlsplit(f"//{authority}")
                hostname = parsed_authority.hostname
                port = parsed_authority.port
            else:
                scheme = request.url.scheme
                hostname = request.url.hostname
                port = request.url.port
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="cross-origin request rejected",
            ) from exc
        if (
            scheme not in {"http", "https"}
            or not hostname
            or any(
                value is not None
                for value in (
                    parsed_authority.username
                    if trusted_proxy and parsed_authority is not None and hostname
                    else None,
                    parsed_authority.password
                    if trusted_proxy and parsed_authority is not None and hostname
                    else None,
                )
            )
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="cross-origin request rejected",
            )
        expected = _normalized_authority(scheme, hostname, port)
        try:
            parsed = urlsplit(origin)
            origin_port = parsed.port
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="cross-origin request rejected",
            ) from exc
        if (
            parsed.scheme.casefold() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="cross-origin request rejected",
            )
        normalized_origin = _normalized_authority(
            parsed.scheme,
            parsed.hostname,
            origin_port,
        )
        if normalized_origin != expected:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="cross-origin request rejected",
            )

    def dashboard_snapshot(
        limit: int,
        cursor: str | None = None,
        scope: ResourceScope | None = None,
        *,
        execution_status: ExecutionStatus | None = None,
        query: str = "",
        include_overview: bool = True,
    ) -> DashboardResponse:
        """读取 Dashboard 快照并把持久化故障转换为统一的 503。

        参数：
            limit: 最近任务数量，路由层的 ``Query`` 已限制为 1 到 100；SSE 使用
                固定值 10。

        返回：
            由服务层生成并映射后的 ``DashboardResponse``。

        异常：
            HTTPException(503): 数据库不可用、状态值损坏或其他 Dashboard 持久化
            错误。不会把数据库连接串、堆栈或凭据放入响应体。
        """
        try:
            snapshot = get_dashboard_service().snapshot(
                limit, cursor, scope, execution_status=execution_status,
                query=query, include_overview=include_overview,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="dashboard cursor is invalid",
            ) from exc
        except DashboardPersistenceError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="dashboard data is temporarily unavailable",
            ) from exc
        return DashboardResponse.from_snapshot(snapshot)

    def dashboard_change_token(scope: ResourceScope | None = None) -> str:
        """读取 SSE 使用的轻量变化令牌，并统一转换持久化错误。"""

        try:
            return get_dashboard_service().change_token(scope=scope)
        except DashboardPersistenceError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="dashboard data is temporarily unavailable",
            ) from exc

    def get_dashboard_stream(
        scope: ResourceScope | None = None,
    ) -> DashboardStreamCoordinator[DashboardResponse]:
        """懒加载同一 API 进程内共享的 Dashboard SSE 轮询器。"""

        if scope is None or scope.unrestricted:
            configured_stream = application.state.dashboard_stream
            if configured_stream is not None:
                return configured_stream
            cache_key = "all"
        else:
            cache_key = scope.cache_key
        registry: DashboardStreamRegistry[DashboardResponse] = (
            application.state.dashboard_streams
        )
        return registry.get_or_create(
            cache_key,
            lambda: DashboardStreamCoordinator(
                lambda: dashboard_change_token(scope),
                lambda: dashboard_snapshot(10, scope=scope),
            ),
        )

    @application.middleware("http")
    async def add_security_headers(request: Request, call_next):
        """为每个响应补充基础安全响应头。

        认证接口额外禁止缓存，避免浏览器或中间代理保留登录结果和会话相关
        响应。更完整的 CSP、TLS 和路径白名单由外层 Nginx 负责。

        参数：
            request: 当前请求，用于判断是否为认证路径。
            call_next: Starlette 提供的下一个处理器，负责真正执行路由。

        返回：
            下游响应对象，附加 ``nosniff``、防点击劫持和 Referrer 策略；认证路径
            额外覆盖为 ``Cache-Control: no-store``。

        中间件不改变响应体，也不承担认证或限流逻辑；异常仍交给 FastAPI/ASGI
        错误处理链处理。
        """
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.url.path.startswith("/api/v1/auth"):
            response.headers["Cache-Control"] = "no-store"
        return response

    error_statuses = {
        ErrorCode.WEBHOOK_INVALID_SIGNATURE: status.HTTP_401_UNAUTHORIZED,
        ErrorCode.WEBHOOK_INVALID_PAYLOAD: status.HTTP_400_BAD_REQUEST,
        ErrorCode.WEBHOOK_PAYLOAD_TOO_LARGE: status.HTTP_413_CONTENT_TOO_LARGE,
        ErrorCode.WEBHOOK_DELIVERY_CONFLICT: status.HTTP_409_CONFLICT,
        ErrorCode.WEBHOOK_NOT_CONFIGURED: status.HTTP_503_SERVICE_UNAVAILABLE,
        ErrorCode.WEBHOOK_PERSISTENCE_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
        ErrorCode.GITHUB_AUTHENTICATION_FAILED: status.HTTP_502_BAD_GATEWAY,
        ErrorCode.GITHUB_PERMISSION_DENIED: status.HTTP_403_FORBIDDEN,
        ErrorCode.GITHUB_NOT_FOUND: status.HTTP_404_NOT_FOUND,
        ErrorCode.GITHUB_RATE_LIMITED: status.HTTP_429_TOO_MANY_REQUESTS,
        ErrorCode.GITHUB_TIMEOUT: status.HTTP_503_SERVICE_UNAVAILABLE,
        ErrorCode.GITHUB_SERVER_ERROR: status.HTTP_503_SERVICE_UNAVAILABLE,
        ErrorCode.GITHUB_REQUEST_REJECTED: status.HTTP_502_BAD_GATEWAY,
        ErrorCode.GITHUB_INVALID_RESPONSE: status.HTTP_502_BAD_GATEWAY,
        ErrorCode.GITHUB_RESPONSE_TOO_LARGE: status.HTTP_502_BAD_GATEWAY,
        ErrorCode.MODEL_AUTHENTICATION_FAILED: status.HTTP_502_BAD_GATEWAY,
        ErrorCode.MODEL_PERMISSION_DENIED: status.HTTP_502_BAD_GATEWAY,
        ErrorCode.MODEL_RATE_LIMITED: status.HTTP_429_TOO_MANY_REQUESTS,
        ErrorCode.MODEL_TIMEOUT: status.HTTP_503_SERVICE_UNAVAILABLE,
        ErrorCode.MODEL_SERVER_ERROR: status.HTTP_503_SERVICE_UNAVAILABLE,
        ErrorCode.MODEL_REQUEST_REJECTED: status.HTTP_502_BAD_GATEWAY,
        ErrorCode.MODEL_INVALID_RESPONSE: status.HTTP_502_BAD_GATEWAY,
        ErrorCode.MODEL_RESPONSE_TOO_LARGE: status.HTTP_502_BAD_GATEWAY,
        ErrorCode.MODEL_OUTPUT_REFUSED: status.HTTP_502_BAD_GATEWAY,
        ErrorCode.MODEL_OUTPUT_TRUNCATED: status.HTTP_502_BAD_GATEWAY,
    }

    @application.exception_handler(SafeApplicationError)
    async def safe_application_error_handler(
        _request: Request,
        error: SafeApplicationError,
    ) -> JSONResponse:
        """只公开稳定且已脱敏的错误契约。"""

        response_status = error_statuses.get(
            error.error.code,
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
        return JSONResponse(
            status_code=response_status,
            content={"error": error.error.public_payload()},
            headers={"Cache-Control": "no-store"},
        )

    @application.get(
        "/healthz",
        response_model=HealthResponse,
        include_in_schema=False,
    )
    async def healthz() -> HealthResponse:
        """返回最小化的进程存活响应。

        返回：
            固定的 ``{"status": "ok", "service": "openreviewer"}`` 模型。

        该路由故意不读取数据库、不验证管理员配置，也不检查 Worker；它只证明
        API 进程和 ASGI 路由仍能响应。数据库/认证可用性由受保护业务接口另行体现。
        """
        return HealthResponse()

    @application.get(
        "/readyz",
        response_model=ReadinessResponse,
        include_in_schema=False,
    )
    async def readyz(response: Response) -> ReadinessResponse:
        """检查数据库连通性、迁移版本和至少一个新鲜 Worker 心跳。"""

        try:
            snapshot = get_operations_service().readiness()
        except (HTTPException, OperationsError, ValueError):
            snapshot = ReadinessSnapshot(
                database=False,
                migration=False,
                worker=False,
            )
        if not snapshot.ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadinessResponse(
            status="ready" if snapshot.ready else "not_ready",
            checks=snapshot.public_checks(),
        )

    @application.get("/metrics", include_in_schema=False)
    async def metrics() -> PlainTextResponse:
        """返回固定低基数的 Prometheus 文本指标。"""

        try:
            content = get_operations_service().metrics() + telemetry.render()
        except (HTTPException, OperationsError, ValueError):
            return PlainTextResponse(
                "# openreviewer database metrics temporarily unavailable\n"
                + telemetry.render(),
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                media_type="text/plain; version=0.0.4",
            )
        return PlainTextResponse(
            content,
            media_type="text/plain; version=0.0.4",
        )

    @application.post(
        "/webhooks/github",
        response_model=WebhookReceiptResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def receive_github_webhook(request: Request) -> WebhookReceiptResponse:
        """限制大小、验签并原子入队一条受支持的 GitHub 投递。"""

        service = get_webhook_service()
        content_type = request.headers.get("content-type", "")
        if content_type.split(";", 1)[0].strip().casefold() != "application/json":
            raise WebhookRequestError(
                SafeError(
                    code=ErrorCode.WEBHOOK_INVALID_PAYLOAD,
                    safe_message="GitHub Webhook 必须使用 application/json",
                    retryable=False,
                )
            )
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                declared_size = int(content_length)
            except ValueError as exc:
                raise WebhookRequestError(
                    SafeError(
                        code=ErrorCode.WEBHOOK_INVALID_PAYLOAD,
                        safe_message="Webhook Content-Length 格式无效",
                        retryable=False,
                    )
                ) from exc
            if declared_size < 0:
                raise WebhookRequestError(
                    SafeError(
                        code=ErrorCode.WEBHOOK_INVALID_PAYLOAD,
                        safe_message="Webhook Content-Length 格式无效",
                        retryable=False,
                    )
                )
            if declared_size > service.settings.max_body_bytes:
                raise WebhookRequestError(
                    SafeError(
                        code=ErrorCode.WEBHOOK_PAYLOAD_TOO_LARGE,
                        safe_message="GitHub Webhook 请求体超过允许大小",
                        retryable=False,
                    )
                )

        body = bytearray()
        async for chunk in request.stream():
            if len(chunk) > service.settings.max_body_bytes - len(body):
                raise WebhookRequestError(
                    SafeError(
                        code=ErrorCode.WEBHOOK_PAYLOAD_TOO_LARGE,
                        safe_message="GitHub Webhook 请求体超过允许大小",
                        retryable=False,
                    )
                )
            body.extend(chunk)
        event_name = request.headers.get("x-github-event", "")
        delivery_id = request.headers.get("x-github-delivery", "")
        signature = request.headers.get("x-hub-signature-256", "")
        receipt = await run_in_threadpool(
            service.receive,
            event_name=event_name,
            delivery_id=delivery_id,
            signature=signature,
            body=bytes(body),
        )
        return WebhookReceiptResponse.from_receipt(receipt)

    @application.post(
        "/api/v1/auth/login",
        response_model=AuthResponse,
    )
    def login(
        credentials: LoginRequest,
        request: Request,
        response: Response,
        _: Annotated[None, Depends(require_same_origin)],
    ) -> AuthResponse:
        """验证管理员凭据并设置 HttpOnly、SameSite 会话 Cookie。

        限流键由客户端地址和大小写折叠后的用户名组成；失败返回统一 401，成功
        后清除失败记录并生成服务端签名会话。密码和 Token 都不会写入响应体之外
        的持久化存储。

        参数：
            credentials: 已经过字段长度、去空白和额外字段校验的用户名/密码体。
            request: 用于提取客户端地址并参与同源校验。
            response: FastAPI 响应对象，用于设置签名会话 Cookie。

        返回：
            ``AuthResponse``，只包含管理员用户名和会话到期时间；Token 仅通过
            HttpOnly Cookie 下发。

        异常：
            HTTPException(403): Origin 与当前入口不一致。
            HTTPException(429): 当前客户端/账号在 15 分钟窗口内失败次数达到上限。
            HTTPException(401): 用户名或密码不匹配，故意不区分具体原因。
            HTTPException(503): 认证配置无法加载。
        """
        client_address = request.client.host if request.client is not None else "unknown"
        if _request_client_is_trusted_proxy(request, trusted_proxy_networks):
            forwarded_address = request.headers.get("x-real-ip", "").strip()
            try:
                client_address = str(ipaddress.ip_address(forwarded_address))
            except ValueError:
                pass
        limiter_key = f"{client_address}|{credentials.username.casefold()}"
        service = get_auth_service()
        limiter = get_login_limiter()
        try:
            limiter.consume(limiter_key)
        except LoginRateLimitError as exc:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="too many login attempts; try again later",
                headers={"Retry-After": str(exc.retry_after_seconds)},
            ) from exc
        except AuthPersistenceError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="authentication state is temporarily unavailable",
                headers={"Cache-Control": "no-store"},
            ) from exc

        try:
            authenticated_user = service.authenticate(
                credentials.username, credentials.password,
            )
        except AuthPersistenceError as exc:
            raise HTTPException(
                status_code=503, detail="authentication state is temporarily unavailable",
                headers={"Cache-Control": "no-store"},
            ) from exc
        if authenticated_user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid username or password",
            )

        try:
            limiter.reset(limiter_key)
            token, principal = service.create_session(authenticated_user)
        except InvalidSessionError as exc:
            raise HTTPException(
                status_code=401, detail="账号已变化，请重新登录",
                headers={"Cache-Control": "no-store"},
            ) from exc
        except AuthPersistenceError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="authentication state is temporarily unavailable",
                headers={"Cache-Control": "no-store"},
            ) from exc
        response.set_cookie(
            key=service.settings.cookie_name,
            value=token,
            max_age=int(service.settings.session_ttl.total_seconds()),
            expires=principal.expires_at,
            path="/",
            secure=service.settings.cookie_secure,
            httponly=True,
            samesite="strict",
        )
        return AuthResponse(
            username=principal.username,
            role=principal.role,
            permissions=tuple(sorted(permissions_for(principal.role), key=str)),
            expires_at=principal.expires_at,
        )

    @application.post(
        "/api/v1/auth/logout",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def logout(
        request: Request,
        _: Annotated[None, Depends(require_same_origin)],
    ) -> Response:
        """要求浏览器删除当前会话 Cookie。

        参数：
            request: 当前请求，保留在签名中以便同源依赖读取其 Origin。
            response: 用于写入过期的 ``Set-Cookie``。

        返回：
            无响应体的 204；即使客户端没有有效 Cookie，删除操作也保持幂等。

        异常：
            HTTPException(403): 请求带有不匹配的 Origin。

        服务端会在共享会话表中吊销当前 Token，同时要求浏览器删除 Cookie；
        因此已经复制出的旧 Token 也不能继续访问管理接口。
        """
        service = get_auth_service()
        token = request.cookies.get(service.settings.cookie_name)
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        response.delete_cookie(
            key=service.settings.cookie_name,
            path="/",
            secure=service.settings.cookie_secure,
            httponly=True,
            samesite="strict",
        )
        try:
            service.revoke_session(token)
        except AuthPersistenceError:
            # 本地 Cookie 仍必须清除，但明确告诉调用方服务端吊销未完成。
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return response

    @application.post(
        "/api/v1/auth/sessions/revoke-all",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def revoke_all_sessions(
        request: Request,
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> Response:
        """吊销当前账号全部会话，并清除浏览器中的当前 Cookie。"""

        service = get_auth_service()
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        response.delete_cookie(
            key=service.settings.cookie_name,
            path="/",
            secure=service.settings.cookie_secure,
            httponly=True,
            samesite="strict",
        )
        try:
            service.revoke_all_sessions(principal.username)
        except AuthPersistenceError:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return response

    @application.get(
        "/api/v1/auth/me",
        response_model=AuthResponse,
    )
    def current_user(
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
    ) -> AuthResponse:
        """返回当前已验证管理员的公开会话信息。

        参数：
            principal: ``require_principal`` 已验证的会话主体。

        返回：
            用户名和绝对到期时间；不返回签名 Token、密码哈希或签名密钥。

        无额外数据库访问，调用失败只可能来自认证依赖或响应模型转换。
        """
        return AuthResponse(
            username=principal.username,
            role=principal.role,
            permissions=tuple(sorted(permissions_for(principal.role), key=str)),
            expires_at=principal.expires_at,
        )

    register_team_routes(
        application,
        get_service=get_team_service,
        require_manager=require_settings_manager,
        require_same_origin=require_same_origin,
    )

    register_evaluation_routes(
        application, get_service=get_evaluation_service,
        require_viewer=require_review_viewer, require_editor=require_adjudicator,
        require_same_origin=require_same_origin,
    )

    register_settings_routes(
        application,
        get_ai_settings_service=get_ai_settings_service,
        get_agent_settings_service=get_agent_settings_service,
        ai_settings_response=ai_settings_response,
        require_settings_manager=require_settings_manager,
        require_same_origin=require_same_origin,
        translate_ai_settings_error=translate_ai_settings_error,
    )


    register_retrieval_routes(
        application, get_service=get_retrieval_service,
        require_manager=require_knowledge_manager,
        require_viewer=require_review_viewer,
        require_same_origin=require_same_origin,
    )

    register_knowledge_routes(
        application,
        get_knowledge_base=get_knowledge_base,
        get_managed_knowledge_base=get_managed_knowledge_base,
        require_knowledge_manager=require_knowledge_manager,
        require_same_origin=require_same_origin,
        translate_knowledge_error=translate_knowledge_error,
    )

    register_dashboard_routes(
        application,
        dashboard_snapshot=dashboard_snapshot,
        get_dashboard_stream=get_dashboard_stream,
        get_auth_service=get_auth_service,
        require_review_viewer=require_review_viewer,
    )

    register_review_routes(
        application,
        get_review_management_service=get_review_management_service,
        get_review_service=get_review_service,
        get_github_access_policy=get_github_access_policy,
        require_review_viewer=require_review_viewer,
        require_review_manager=require_review_manager,
        require_adjudicator=require_adjudicator,
        require_same_origin=require_same_origin,
        ensure_permission=ensure_permission,
    )

    return application


app = create_app()
