import os
from datetime import timedelta

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from domain.enums import ExecutionStatus, PullRequestAction
from domain.models import PullRequestWebhook, ReviewRequest
from persistence.database import Database
from persistence.models import (
    Base,
    GitHubWebhookDeliveryRecord,
    ReviewRunRecord,
    ReviewTaskRecord,
)
from persistence.repositories import SqlAlchemyReviewRepository
from persistence.task_queue import SqlAlchemyReviewTaskQueue
from persistence.webhooks import SqlAlchemyGitHubWebhookRepository
from services.reviews import ReviewService


@pytest.fixture(scope="module")
def postgres_database():
    raw_url = os.environ.get("OPENREVIEWER_TEST_POSTGRES_URL", "").strip()
    if not raw_url:
        pytest.skip("未配置隔离的 PostgreSQL 契约测试数据库")
    url = make_url(raw_url)
    if url.host not in {"127.0.0.1", "localhost"} or url.database != "openreviewer_test":
        raise RuntimeError(
            "PostgreSQL 契约测试只允许使用本机 openreviewer_test 数据库"
        )

    database = Database.connect(url)
    try:
        Base.metadata.drop_all(database.engine)
        with database.engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
        configuration = Config("alembic.ini")
        configuration.set_main_option(
            "sqlalchemy.url", url.render_as_string(hide_password=False)
        )
        command.upgrade(configuration, "head")
        command.check(configuration)
        yield database
    finally:
        Base.metadata.drop_all(database.engine)
        with database.engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
        database.dispose()


def test_postgres_migrations_and_skip_locked_claim(postgres_database: Database) -> None:
    submission = ReviewService(
        SqlAlchemyReviewRepository(postgres_database.sessions)
    ).submit(
        ReviewRequest(
            installation_id=10,
            repository_id=42,
            repository="lboverfys/NiuMa",
            pull_request_number=128,
            head_sha="a" * 40,
        ),
        "postgres-lock-contract",
    )
    queue = SqlAlchemyReviewTaskQueue(postgres_database.sessions)

    with postgres_database.sessions() as blocker:
        transaction = blocker.begin()
        locked_task = blocker.scalar(
            select(ReviewTaskRecord)
            .where(ReviewTaskRecord.id == submission.review_task_id)
            .with_for_update()
        )
        assert locked_task is not None
        assert queue.claim_next("worker-2", timedelta(seconds=30)) is None
        transaction.rollback()

    lease = queue.claim_next("worker-1", timedelta(seconds=30))
    assert lease is not None
    assert lease.task_id == submission.review_task_id
    with postgres_database.sessions() as session:
        task = session.get(ReviewTaskRecord, submission.review_task_id)
        assert task.execution_status == ExecutionStatus.RUNNING.value


def test_postgres_webhook_creation_respects_foreign_keys(
    postgres_database: Database,
) -> None:
    repository = SqlAlchemyGitHubWebhookRepository(postgres_database.sessions)
    event = PullRequestWebhook(
        action=PullRequestAction.OPENED,
        delivery_id="postgres-delivery-001",
        installation_id=20,
        repository_id=43,
        repository="lboverfys/NiuMa",
        pull_request_number=129,
        head_sha="b" * 40,
    )

    created = repository.create_or_get(event, "c" * 64)
    repeated = repository.create_or_get(event, "c" * 64)

    assert created.created is True
    assert repeated.created is False
    assert repeated.review_run_id == created.review_run_id
    assert repeated.review_task_id == created.review_task_id
    with postgres_database.sessions() as session:
        persisted = session.execute(
            select(
                GitHubWebhookDeliveryRecord.delivery_id,
                ReviewRunRecord.id,
                ReviewTaskRecord.id,
            )
            .join(
                ReviewRunRecord,
                ReviewRunRecord.id == GitHubWebhookDeliveryRecord.review_run_id,
            )
            .join(
                ReviewTaskRecord,
                ReviewTaskRecord.id == GitHubWebhookDeliveryRecord.review_task_id,
            )
            .where(
                GitHubWebhookDeliveryRecord.delivery_id
                == "postgres-delivery-001"
            )
        ).one()
    assert persisted == (
        "postgres-delivery-001",
        created.review_run_id,
        created.review_task_id,
    )
