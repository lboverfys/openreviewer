import asyncio
from datetime import datetime
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select

from apps.api.main import create_app
from persistence.database import Database
from persistence.models import Base, OutboxEventRecord, ReviewRunRecord, ReviewTaskRecord
from persistence.repositories import SqlAlchemyReviewRepository
from services.reviews import ReviewService
from tests.support import TEST_PASSWORD, TEST_USERNAME, make_auth_service


HEAD_SHA = "a" * 40


@pytest.fixture
def database(tmp_path: Path):
    """为每个任务 API 用例创建隔离 SQLite 数据库和完整表结构。

    参数：
        tmp_path: pytest 提供的当前用例临时目录。

    产生：
        可通过真实 SQLAlchemy 仓储读写的 ``Database``；用例结束时释放连接池，
        临时文件随后由 pytest 清理，不会污染项目目录。
    """
    database_path = (tmp_path / "reviews.sqlite3").as_posix()
    configured_database = Database.connect(f"sqlite:///{database_path}")
    Base.metadata.create_all(configured_database.engine)
    try:
        yield configured_database
    finally:
        configured_database.dispose()


def review_payload(**overrides: object) -> dict[str, object]:
    """生成合法任务请求字典，并允许测试覆盖单个字段。

    参数：
        overrides: 要替换或新增的请求字段。

    返回：
        默认指向固定安装、仓库、PR 和大写 SHA 的可变字典。大写 SHA 用来同时
        验证 API/领域层会规范化输入；覆盖项用于构造非法格式或不同请求内容。
    """
    payload: dict[str, object] = {
        "installation_id": 10,
        "repository_id": 42,
        "repository": "lboverfys/NiuMa",
        "pull_request_number": 128,
        "head_sha": HEAD_SHA.upper(),
    }
    payload.update(overrides)
    return payload


async def post_review(
    application,
    payload: dict[str, object],
    idempotency_key: str | None,
) -> httpx.Response:
    """模拟管理员登录后提交一次审查请求。

    参数：
        application: 要测试的 FastAPI 应用。
        payload: 任务 JSON 请求体，可以是合法或刻意非法数据。
        idempotency_key: 要放入请求头的键；``None`` 用于验证缺失请求头。

    返回：
        POST ``/api/v1/reviews`` 的 httpx 响应。

    辅助函数先断言测试登录成功，避免后续 401 掩盖真正要验证的任务 API 行为；
    同一客户端自动保存会话 Cookie。
    """
    headers = (
        {"Idempotency-Key": idempotency_key}
        if idempotency_key is not None
        else None
    )
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        login = await client.post(
            "/api/v1/auth/login",
            json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
        )
        assert login.status_code == 200
        return await client.post(
            "/api/v1/reviews",
            json=payload,
            headers=headers,
        )


def app_for(database: Database):
    """构造任务 API 集成测试应用。

    参数：
        database: 当前用例的隔离数据库。

    返回：
        使用真实 ``SqlAlchemyReviewRepository`` 和测试认证服务的 FastAPI 应用，
        从而覆盖 HTTP、Pydantic、服务指纹和数据库事务整条链路。
    """
    repository = SqlAlchemyReviewRepository(database.sessions)
    return create_app(
        ReviewService(repository),
        auth_service=make_auth_service(),
    )


def test_create_review_persists_run_task_and_outbox_atomically(
    database: Database,
) -> None:
    """验证首次请求会原子创建运行、任务和 Outbox 事件。

    参数：
        database: 当前用例数据库。

    动作：登录并用合法请求/新幂等键提交一次任务。
    预期：返回 202、合法 UUID、规范版本键、queued 状态和 created=true；数据库
    三张表各有一行，事件 payload 中的三个身份与响应完全一致。
    """
    response = asyncio.run(
        post_review(app_for(database), review_payload(), "manual-request-001")
    )

    assert response.status_code == 202
    body = response.json()
    assert UUID(body["review_run_id"])
    assert UUID(body["review_task_id"])
    assert body["review_version_key"] == f"42:128:{HEAD_SHA}"
    assert body["execution_status"] == "queued"
    assert datetime.fromisoformat(body["accepted_at"])
    assert body["created"] is True

    with database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ReviewRunRecord)) == 1
        assert session.scalar(select(func.count()).select_from(ReviewTaskRecord)) == 1
        assert session.scalar(select(func.count()).select_from(OutboxEventRecord)) == 1
        event = session.scalar(select(OutboxEventRecord))
        assert event is not None
        assert event.event_type == "review.requested"
        assert event.payload == {
            "review_run_id": body["review_run_id"],
            "review_task_id": body["review_task_id"],
            "review_version_key": body["review_version_key"],
        }


def test_same_idempotency_key_returns_the_original_task(
    database: Database,
) -> None:
    """验证相同幂等键和相同请求体会返回原任务。

    参数：
        database: 当前用例数据库。

    动作：向同一应用连续提交两次相同请求和幂等键。
    预期：两次都是 202，第二次除 ``created=false`` 外与第一次相同；运行、任务、
    事件仍各一行，证明重试不会重复产生副作用。
    """
    application = app_for(database)
    first = asyncio.run(
        post_review(application, review_payload(), "manual-request-002")
    )
    repeated = asyncio.run(
        post_review(application, review_payload(), "manual-request-002")
    )

    assert first.status_code == repeated.status_code == 202
    assert repeated.json() == {**first.json(), "created": False}
    with database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ReviewRunRecord)) == 1
        assert session.scalar(select(func.count()).select_from(ReviewTaskRecord)) == 1
        assert session.scalar(select(func.count()).select_from(OutboxEventRecord)) == 1


def test_idempotency_key_reuse_with_different_content_is_rejected(
    database: Database,
) -> None:
    """验证同一幂等键不能绑定不同的规范请求体。

    参数：
        database: 当前用例数据库。

    动作：先创建 head A 的任务，再用相同键提交 head B。
    预期：第二次返回稳定 409 提示，数据库仍只有第一条运行；这证明请求指纹参与
    幂等判定，不能把错误重用键静默当成第一次请求。
    """
    application = app_for(database)
    first = asyncio.run(
        post_review(application, review_payload(), "manual-request-003")
    )
    conflict = asyncio.run(
        post_review(
            application,
            review_payload(head_sha="b" * 40),
            "manual-request-003",
        )
    )

    assert first.status_code == 202
    assert conflict.status_code == 409
    assert conflict.json() == {
        "detail": "Idempotency-Key was already used for a different request"
    }
    with database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ReviewRunRecord)) == 1


def test_new_idempotency_key_explicitly_creates_another_review_run(
    database: Database,
) -> None:
    """验证新幂等键可以显式重新审查同一个提交版本。

    参数：
        database: 当前用例数据库。

    动作：请求体完全相同，但分别使用 ``rerun-001`` 和 ``rerun-002``。
    预期：生成不同运行/任务 ID，三张表各有两行；版本键相同不构成唯一约束，
    因为一次提交允许管理员明确触发多次运行。
    """
    application = app_for(database)
    first = asyncio.run(post_review(application, review_payload(), "rerun-001"))
    second = asyncio.run(post_review(application, review_payload(), "rerun-002"))

    assert first.status_code == second.status_code == 202
    assert first.json()["review_run_id"] != second.json()["review_run_id"]
    assert first.json()["review_task_id"] != second.json()["review_task_id"]
    with database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ReviewRunRecord)) == 2
        assert session.scalar(select(func.count()).select_from(ReviewTaskRecord)) == 2
        assert session.scalar(select(func.count()).select_from(OutboxEventRecord)) == 2


@pytest.mark.parametrize(
    ("payload", "idempotency_key"),
    [
        (review_payload(head_sha="short"), "invalid-sha"),
        (review_payload(repository="not-a-full-name"), "invalid-repository"),
        ({**review_payload(), "unexpected": True}, "extra-field"),
        (review_payload(), None),
    ],
)
def test_invalid_review_request_is_rejected_before_persistence(
    database: Database,
    payload: dict[str, object],
    idempotency_key: str | None,
) -> None:
    """验证各种契约错误会在持久化前统一返回 422。

    参数：
        database: 当前用例数据库。
        payload: 参数化传入的非法 SHA、仓库名、额外字段或普通合法请求体。
        idempotency_key: 对应的请求头；最后一个参数组合故意传 ``None``。

    预期：每种情况均返回 422，运行表保持 0 行，证明 FastAPI/Pydantic 校验不会
    留下半成品数据库记录。
    """
    response = asyncio.run(
        post_review(app_for(database), payload, idempotency_key)
    )

    assert response.status_code == 422
    with database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ReviewRunRecord)) == 0


def test_unconfigured_persistence_returns_service_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证认证可用但数据库未配置时返回可安全重试的 503。

    参数：
        monkeypatch: pytest 环境变量隔离工具，用于临时删除数据库配置。

    动作：创建只注入认证服务的应用并提交合法任务。
    预期：懒加载数据库时返回稳定 503，而不是 500 或假 202；客户端可以保留原
    幂等键在配置恢复后重试。monkeypatch 会在用例后恢复原环境。
    """
    for variable in (
        "OPENREVIEWER_DATABASE_URL",
        "OPENREVIEWER_DB_PASSWORD",
    ):
        monkeypatch.delenv(variable, raising=False)

    response = asyncio.run(
        post_review(
            create_app(auth_service=make_auth_service()),
            review_payload(),
            "no-database",
        )
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "review persistence is not configured"}
