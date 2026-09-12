"""队列阶段操作共享的存储上下文，不包含业务调度。"""

from collections.abc import Callable
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker


class QueueStorage(Protocol):
    _sessions: sessionmaker[Session]
    _clock: Callable[[], datetime]
    _uuid_factory: Callable[[], UUID]
    _retry_base_seconds: int
    _retry_cap_seconds: int
    _recovery_batch_size: int
