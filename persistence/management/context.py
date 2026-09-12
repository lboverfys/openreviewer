"""管理存储操作的依赖上下文。"""

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from services.review_management import StoredReviewDetails


class ManagementStorage(Protocol):
    _sessions: sessionmaker[Session]
    _clock: Callable[[], datetime]
    _uuid_factory: Callable[[], object]
    _publisher: Callable[[StoredReviewDetails], None] | None
