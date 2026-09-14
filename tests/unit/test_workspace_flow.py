from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import event, update

from domain.evaluation_workbench import EvaluationDatasetCreate, EvaluationNotFoundError
from domain.retrieval import RetrievalSettings
from persistence.database import Database
from persistence.models import Base, EvaluationObservationRecord
from persistence.retrieval import RetrievalRepository
from services.ai_settings import AiSecretCipher
from services.evaluation_workbench import EvaluationWorkbench
from services.rag import ManagedMarkdownKnowledgeBase, MarkdownKnowledgeBase
from services.rbac import ResourceScope
from services.retrieval import HybridRetrievalService, RetrievalSettingsService
from services.retrieval_providers import external_retrieval_paused
from tests.evaluation_support import seed_evaluation_runs


@pytest.fixture
def database(tmp_path: Path):
    db = Database.connect(f"sqlite:///{(tmp_path / 'workspace.sqlite3').as_posix()}")
    Base.metadata.create_all(db.engine)
    try:
        yield db
    finally:
        db.dispose()


@pytest.mark.parametrize(("legacy", "enabled", "paused"), [
    ("true", None, True), ("false", None, False),
    ("true", True, False), ("false", False, True),
])
def test_saved_retrieval_switch_overrides_legacy_default(monkeypatch, legacy, enabled, paused):
    monkeypatch.setenv("OPENREVIEWER_RETRIEVAL_API_DISABLED", legacy)
    assert external_retrieval_paused(RetrievalSettings(external_calls_enabled=enabled)) is paused


def test_new_seed_files_are_not_automatically_imported_and_manual_create_works(database, tmp_path):
    seeds = tmp_path / "knowledge"
    seeds.mkdir()
    (seeds / "common.md").write_text("# 原有规则\n\n保留人工约定。", encoding="utf-8")
    original = ManagedMarkdownKnowledgeBase(database.sessions, seeds)
    library = original.list_documents()
    (seeds / "game.md").write_text(
        "# 游戏资格\n\n适用仓库：lboverfys/NiuMa\n\n游戏资格由管理员考核后授予。", encoding="utf-8",
    )
    knowledge = ManagedMarkdownKnowledgeBase(database.sessions, seeds)
    assert knowledge.list_documents().total == 1
    assert not hasattr(knowledge, "install_project_pack")
    updated = knowledge.create_document(source="game.md", content=(seeds / "game.md").read_text(encoding="utf-8"),
        enabled=True, expected_revision=library.revision, actor="tester", repository_scope="lboverfys/NiuMa")
    game = updated.document
    assert game.repository_scope == "lboverfys/niuma"
    assert knowledge.get_document(library.items[0].id).content == "# 原有规则\n\n保留人工约定。"
    changed = knowledge.update_document(game.id, source=game.source,
        content=knowledge.get_document(game.id).content, enabled=True, expected_revision=updated.revision,
        expected_document_version=1, actor="tester", repository_scope="example/other", update_repository_scope=True)
    assert changed.document.repository_scope == "example/other"
    assert changed.document.current_version == 2


def test_retrieval_decimal_prices_roundtrip_and_connection_fingerprint(database):
    service = RetrievalSettingsService(database.sessions, AiSecretCipher(b"k" * 32))
    settings = RetrievalSettings(api_host="https://dashscope.aliyuncs.com",
        embedding_usd_per_million=Decimal("0.074536"), rerank_usd_per_million=Decimal("0.074536"),
        external_calls_enabled=False)
    saved = service.update(settings, 0, "tester")
    assert saved.settings.embedding_usd_per_million == Decimal("0.074536")
    assert saved.external_calls_paused
    service.mark_tested(saved)
    assert service.get().tested


def test_current_switch_controls_frozen_profile_retrieval(database, monkeypatch):
    from domain.retrieval import RetrievalSettingsView
    from tests.unit.test_model_review import make_model_input
    settings = RetrievalSettingsService(database.sessions, AiSecretCipher(b"k" * 32))
    settings.update(RetrievalSettings(api_host="https://dashscope.aliyuncs.com", external_calls_enabled=False), 0, "tester")
    service = HybridRetrievalService(RetrievalRepository(database.sessions), settings)
    captured = []
    def capture(model_input, _progress, *, frozen_runtime):
        captured.append(frozen_runtime[0])
        return model_input
    monkeypatch.setattr(service, "_review_context", capture)
    frozen = RetrievalSettingsView(revision=1, key_configured=True, external_calls_paused=False,
        settings=RetrievalSettings(api_host="https://dashscope.aliyuncs.com", external_calls_enabled=True, context_k=3))
    source = make_model_input()
    assert service.review_context(source, lambda: None, frozen_runtime=(frozen, "fixture-key")) is source
    assert captured[0].external_calls_paused and captured[0].settings.external_calls_enabled is False
    assert captured[0].settings.context_k == 3


def test_single_baseline_review_progress_does_not_require_candidate(database):
    identifiers = seed_evaluation_runs(database, [dict(label="baseline", pr=93, findings=["问题甲", "问题乙"])])
    workbench = EvaluationWorkbench(database.sessions)
    scope = ResourceScope(repositories=frozenset({"lboverfys/niuma"}))
    dataset = workbench.create_dataset(EvaluationDatasetCreate(name="单组复核", review_run_ids=(identifiers["baseline"],)),
        "workspace-overview", "tester", scope)
    first = workbench.overview(dataset.id, scope)
    assert (first.case_count, first.observation_count, first.reviewed_observations) == (1, 1, 0)
    assert first.unreviewed_findings == 2 and first.missing_reference_cases == 1
    with database.sessions() as session, session.begin():
        session.execute(update(EvaluationObservationRecord).where(
            EvaluationObservationRecord.source_run_id == identifiers["baseline"],
        ).values(assessment_status="complete",
            metrics={"adjudicated_count": 2, "valid_count": 1, "false_positive_count": 1}))
    statements = []
    def count(_conn, _cursor, sql, _params, _context, _many):
        statements.append(sql)
    event.listen(database.engine, "before_cursor_execute", count)
    try:
        completed = workbench.overview(dataset.id, scope)
    finally:
        event.remove(database.engine, "before_cursor_execute", count)
    assert len(statements) == 2
    assert completed.reviewed_observations == 1
    assert completed.valid_findings == 1 and completed.false_positive_findings == 1
    assert completed.unreviewed_findings == 0
    assert all("source_snapshot" not in sql for sql in statements)
    with pytest.raises(EvaluationNotFoundError):
        workbench.overview(dataset.id, ResourceScope(repositories=frozenset({"another/repo"})))


def test_project_knowledge_contains_real_business_rules():
    library = MarkdownKnowledgeBase("knowledge")
    hits = library.search("游戏资格 管理员 报价", limit=5)
    assert any(item.source == "niuma/companion-games.md" for item in hits)
