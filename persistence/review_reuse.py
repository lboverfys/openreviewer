"""按精确输入哈希命中的有界复用存储；租约校验与保存处于同一事务。"""

from datetime import timedelta

from sqlalchemy import select

from persistence.models import ReviewReuseRecord
from persistence.platform_common import dialect_insert
from persistence.queue.common import _locked_owned_task_with_run


def load_reused_review(queue, lease, key):
    now = queue._clock()
    with queue._sessions() as session:
        _locked_owned_task_with_run(session, lease, now)
        row = session.execute(select(ReviewReuseRecord.source_run_id,
            ReviewReuseRecord.head_sha, ReviewReuseRecord.result).where(
            ReviewReuseRecord.key == key,
            ReviewReuseRecord.source_run_id != lease.review_run_id,
            ReviewReuseRecord.created_at >= now - timedelta(days=7),
        )).one_or_none()
        return tuple(row) if row else None


def store_reusable_review(queue, lease, key, head_sha, payload):
    now = queue._clock()
    with queue._sessions() as session, session.begin():
        _locked_owned_task_with_run(session, lease, now)
        values = dict(key=key, source_run_id=lease.review_run_id, head_sha=head_sha,
                      result=payload, created_at=now)
        statement = dialect_insert(session, ReviewReuseRecord).values(**values)
        session.execute(statement.on_conflict_do_update(index_elements=["key"], set_=values))
