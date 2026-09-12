"""团队管理功能的依赖组合；读取页面无需构造模型客户端。"""

from dataclasses import dataclass

from persistence.platform_queries import PlatformQueries
from persistence.review_profiles import ReviewProfileRepository
from persistence.usage_queries import UsageQueries
from persistence.work_items import WorkItemRepository


@dataclass(frozen=True, slots=True)
class PlatformServices:
    usage: UsageQueries
    work_items: WorkItemRepository
    queries: PlatformQueries
    profiles: ReviewProfileRepository
