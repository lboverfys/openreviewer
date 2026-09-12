"""把人工处理结论转成待审核的仓库知识；不自动启用、不调用模型。"""

from domain.platform import (
    KnowledgeProposalView,
    KnowledgeProposalWrite,
    PlatformConflictError,
)
from domain.security import redact_sensitive
from persistence.work_items import WorkItemRepository
from services.rag import (
    KnowledgeConflictError,
    KnowledgeValidationError,
    ManagedMarkdownKnowledgeBase,
)
from services.rbac import ResourceScope


class ReviewLearningService:
    def __init__(
        self, work_items: WorkItemRepository, knowledge: ManagedMarkdownKnowledgeBase
    ):
        self.work_items, self.knowledge = work_items, knowledge

    def propose(
        self,
        identifier: str,
        draft: KnowledgeProposalWrite,
        actor: str,
        scope: ResourceScope,
    ) -> KnowledgeProposalView:
        item = self.work_items.learning_source(
            identifier, draft.expected_work_revision, scope
        )
        if item.status not in {"resolved", "wont_fix"}:
            raise ValueError("请先保存人工处理结论，再整理为知识草稿")
        source = f"lessons/{item.id}.md"
        content = f"""# 审查经验：{item.title}

适用仓库：{item.repository}

## 人工总结

{draft.lesson}

## 来源与边界

- 工作项：{item.id}，版本 {item.revision}
- 来源审查：{item.source_run_id}
- 来源问题：{item.source_finding_id}
- 业务 PR：https://github.com/{item.repository}/pull/{item.pull_request_number}
- 人工处理状态：{item.status}
- 修复 PR：{item.fix_pull_request_number or "未关联"}
- 处理说明：{item.note}

此文档来自人工记录，只适用于上述仓库。后续修改工作项不会自动覆盖本知识版本。
"""
        try:
            saved = self.knowledge.create_document(
                source=source,
                content=str(redact_sensitive(content)),
                enabled=False,
                expected_revision=draft.expected_library_revision,
                actor=actor,
                repository_scope=item.repository,
            )
        except (KnowledgeConflictError, KnowledgeValidationError) as exc:
            raise PlatformConflictError(str(exc)) from exc
        return KnowledgeProposalView(
            document_id=saved.document.id,
            source=source,
            library_revision=saved.revision,
        )
