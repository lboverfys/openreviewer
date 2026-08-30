"""知识库管理路由。"""

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Path, Query, status

from apps.api.schemas import (
    KnowledgeCitationResponse,
    KnowledgeDocumentCreateRequest,
    KnowledgeDocumentListResponse,
    KnowledgeDocumentResponse,
    KnowledgeDocumentStateRequest,
    KnowledgeDocumentUpdateRequest,
    KnowledgeMutationResponse,
    KnowledgeSearchResponse,
)
from services.auth import SessionPrincipal
from services.rag import (
    KnowledgeConflictError,
    KnowledgeNotFoundError,
    KnowledgePersistenceError,
    KnowledgeValidationError,
    ManagedMarkdownKnowledgeBase,
    MarkdownKnowledgeBase,
)


def register_knowledge_routes(
    application: FastAPI,
    *,
    get_knowledge_base: Callable[[], MarkdownKnowledgeBase],
    get_managed_knowledge_base: Callable[[], ManagedMarkdownKnowledgeBase],
    require_knowledge_manager: Callable[..., SessionPrincipal],
    require_same_origin: Callable[..., None],
    translate_knowledge_error: Callable[[Exception], HTTPException],
) -> None:
    """注册知识检索和版本化文档管理端点。"""

    @application.get(
        "/api/v1/knowledge/search",
        response_model=KnowledgeSearchResponse,
    )
    def search_knowledge(
        q: Annotated[str, Query(min_length=1, max_length=500)],
        _: Annotated[SessionPrincipal, Depends(require_knowledge_manager)],
        limit: Annotated[int, Query(ge=1, le=20)] = 5,
    ) -> KnowledgeSearchResponse:
        try:
            items = get_knowledge_base().search(q, limit=limit)
        except (KnowledgePersistenceError, KnowledgeValidationError) as exc:
            raise translate_knowledge_error(exc) from exc
        return KnowledgeSearchResponse(
            query=q,
            items=tuple(
                KnowledgeCitationResponse(
                    **{
                        name: getattr(item, name)
                        for name in KnowledgeCitationResponse.model_fields
                    }
                )
                for item in items
            ),
        )

    @application.get(
        "/api/v1/knowledge/documents",
        response_model=KnowledgeDocumentListResponse,
    )
    def list_knowledge_documents(
        _: Annotated[SessionPrincipal, Depends(require_knowledge_manager)],
        include_archived: bool = False,
        limit: Annotated[int, Query(ge=1, le=128)] = 128,
    ) -> KnowledgeDocumentListResponse:
        try:
            view = get_managed_knowledge_base().list_documents(
                include_archived=include_archived,
                limit=limit,
            )
        except (
            KnowledgeConflictError,
            KnowledgePersistenceError,
            KnowledgeValidationError,
        ) as exc:
            raise translate_knowledge_error(exc) from exc
        return KnowledgeDocumentListResponse.from_view(view)

    @application.post(
        "/api/v1/knowledge/documents",
        response_model=KnowledgeMutationResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_knowledge_document(
        request_body: KnowledgeDocumentCreateRequest,
        principal: Annotated[
            SessionPrincipal,
            Depends(require_knowledge_manager),
        ],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> KnowledgeMutationResponse:
        try:
            view = get_managed_knowledge_base().create_document(
                source=request_body.source,
                content=request_body.content,
                enabled=request_body.enabled,
                expected_revision=request_body.expected_revision,
                actor=principal.username,
            )
        except (
            KnowledgeConflictError,
            KnowledgePersistenceError,
            KnowledgeValidationError,
        ) as exc:
            raise translate_knowledge_error(exc) from exc
        return KnowledgeMutationResponse.from_view(view)

    @application.get(
        "/api/v1/knowledge/documents/{document_id}",
        response_model=KnowledgeDocumentResponse,
    )
    def get_knowledge_document(
        document_id: str,
        _: Annotated[SessionPrincipal, Depends(require_knowledge_manager)],
    ) -> KnowledgeDocumentResponse:
        try:
            view = get_managed_knowledge_base().get_document(document_id)
        except (KnowledgeNotFoundError, KnowledgePersistenceError) as exc:
            raise translate_knowledge_error(exc) from exc
        return KnowledgeDocumentResponse.from_document_view(view)

    @application.put(
        "/api/v1/knowledge/documents/{document_id}",
        response_model=KnowledgeMutationResponse,
    )
    def update_knowledge_document(
        document_id: str,
        request_body: KnowledgeDocumentUpdateRequest,
        principal: Annotated[
            SessionPrincipal,
            Depends(require_knowledge_manager),
        ],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> KnowledgeMutationResponse:
        try:
            view = get_managed_knowledge_base().update_document(
                document_id,
                source=request_body.source,
                content=request_body.content,
                enabled=request_body.enabled,
                expected_revision=request_body.expected_revision,
                expected_document_version=request_body.expected_document_version,
                actor=principal.username,
            )
        except (
            KnowledgeConflictError,
            KnowledgeNotFoundError,
            KnowledgePersistenceError,
            KnowledgeValidationError,
        ) as exc:
            raise translate_knowledge_error(exc) from exc
        return KnowledgeMutationResponse.from_view(view)

    @application.post(
        "/api/v1/knowledge/documents/{document_id}/archive",
        response_model=KnowledgeMutationResponse,
    )
    def archive_knowledge_document(
        document_id: str,
        request_body: KnowledgeDocumentStateRequest,
        principal: Annotated[
            SessionPrincipal,
            Depends(require_knowledge_manager),
        ],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> KnowledgeMutationResponse:
        try:
            view = get_managed_knowledge_base().archive_document(
                document_id,
                archived=True,
                expected_revision=request_body.expected_revision,
                expected_document_version=request_body.expected_document_version,
                actor=principal.username,
            )
        except (
            KnowledgeConflictError,
            KnowledgeNotFoundError,
            KnowledgePersistenceError,
        ) as exc:
            raise translate_knowledge_error(exc) from exc
        return KnowledgeMutationResponse.from_view(view)

    @application.post(
        "/api/v1/knowledge/documents/{document_id}/restore",
        response_model=KnowledgeMutationResponse,
    )
    def restore_knowledge_document(
        document_id: str,
        request_body: KnowledgeDocumentStateRequest,
        principal: Annotated[
            SessionPrincipal,
            Depends(require_knowledge_manager),
        ],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> KnowledgeMutationResponse:
        try:
            view = get_managed_knowledge_base().archive_document(
                document_id,
                archived=False,
                expected_revision=request_body.expected_revision,
                expected_document_version=request_body.expected_document_version,
                actor=principal.username,
            )
        except (
            KnowledgeConflictError,
            KnowledgeNotFoundError,
            KnowledgePersistenceError,
        ) as exc:
            raise translate_knowledge_error(exc) from exc
        return KnowledgeMutationResponse.from_view(view)

    @application.post(
        "/api/v1/knowledge/documents/{document_id}/versions/{version}/restore",
        response_model=KnowledgeMutationResponse,
    )
    def restore_knowledge_document_version(
        document_id: str,
        version: Annotated[int, Path(ge=1)],
        request_body: KnowledgeDocumentStateRequest,
        principal: Annotated[
            SessionPrincipal,
            Depends(require_knowledge_manager),
        ],
        _: Annotated[None, Depends(require_same_origin)],
    ) -> KnowledgeMutationResponse:
        try:
            view = get_managed_knowledge_base().restore_version(
                document_id,
                version,
                expected_revision=request_body.expected_revision,
                expected_document_version=request_body.expected_document_version,
                actor=principal.username,
            )
        except (
            KnowledgeConflictError,
            KnowledgeNotFoundError,
            KnowledgePersistenceError,
            KnowledgeValidationError,
        ) as exc:
            raise translate_knowledge_error(exc) from exc
        return KnowledgeMutationResponse.from_view(view)
