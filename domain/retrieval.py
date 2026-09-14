"""代码索引、检索过程与评测的可序列化契约。"""

from __future__ import annotations

import json
from decimal import Decimal
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from domain.enums import ReviewAgent
from domain.identifiers import normalize_sha
from domain.paths import normalize_repository_path

PARSER_VERSION = "java-mybatis-v2"
# 文本格式未变；与旧向量缓存保持同一命名空间，仅内容改变的片段需重算。
EMBEDDING_CACHE_VERSION = "java-mybatis-v1"
VECTOR_DIMENSIONS = 1024
MAX_INDEX_CHUNKS = 20_000
MAX_INDEX_FILES = 1_000
MAX_SOURCE_BYTES = 256 * 1024
MAX_CHUNK_CHARS = 6_000

RetrievalStrategy = Literal["bm25", "lexical_relations", "hybrid", "hybrid_relations", "reranked"]
AnnotationSource = Literal["synthetic_contract", "agent_annotated", "independent_human", "single_reviewer"]
RetrievalRoute = Literal["bm25", "vector", "relation"]


def stable_key(*parts: object) -> str:
    return sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class RetrievalContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceFile(RetrievalContract):
    file: str = Field(min_length=1, max_length=1024)
    blob_sha: str
    content: str = Field(max_length=MAX_SOURCE_BYTES)

    _path = field_validator("file")(normalize_repository_path)
    _sha = field_validator("blob_sha")(normalize_sha)


class CodeChunk(RetrievalContract):
    id: str
    file: str
    blob_sha: str
    language: str
    kind: str
    symbol: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    content: str = Field(max_length=MAX_CHUNK_CHARS)
    content_hash: str
    aliases: tuple[str, ...] = ()
    references: tuple[str, ...] = ()
    fragment: int = 0
    parse_error: bool = False

    @property
    def embedding_text(self) -> str:
        return f"{self.language} {self.symbol}\n{self.content}"

    @property
    def embedding_hash(self) -> str:
        return sha256(self.embedding_text.encode()).hexdigest()


class CodeRelation(RetrievalContract):
    source_id: str
    target_id: str
    kind: Literal["reference", "mapper_sql"]


class ParsedSources(RetrievalContract):
    chunks: tuple[CodeChunk, ...]
    relations: tuple[CodeRelation, ...]
    unresolved_references: int = 0
    parse_error_files: tuple[str, ...] = ()


class RetrievalSettings(RetrievalContract):
    enabled: bool = False
    # 未在页面保存过开关时兼容原部署值；保存后由管理员页面控制。
    external_calls_enabled: bool | None = None
    api_host: str = ""
    embedding_model: str = Field(default="qwen3.7-text-embedding", min_length=1, max_length=200)
    rerank_model: str = Field(default="qwen3.7-text-rerank", min_length=1, max_length=200)
    dimensions: Literal[1024] = 1024
    strategy: RetrievalStrategy = "reranked"
    candidate_k: int = Field(default=20, ge=1, le=50)
    context_k: int = Field(default=8, ge=1, le=20)
    timeout_seconds: int = Field(default=60, ge=5, le=180)
    max_new_vectors_per_index: int = Field(default=100, ge=0, le=20_000)
    max_requests_per_operation: int = Field(default=12, ge=0, le=300)
    embedding_usd_per_million: Decimal | None = Field(default=None, ge=0, le=1_000_000)
    rerank_usd_per_million: Decimal | None = Field(default=None, ge=0, le=1_000_000)

    @property
    def embedding_fingerprint(self) -> str:
        return stable_key(self.api_host, self.embedding_model, self.dimensions, EMBEDDING_CACHE_VERSION)

    def index_key(self, installation_id: int, repository_id: int, head_sha: str) -> str:
        return stable_key(installation_id, repository_id, head_sha, self.embedding_fingerprint, PARSER_VERSION)


class RetrievalSettingsView(RetrievalContract):
    revision: int
    settings: RetrievalSettings
    key_configured: bool
    tested: bool = False
    external_calls_paused: bool = False


class SearchQuery(RetrievalContract):
    query: str = Field(min_length=1, max_length=4000)
    seed_files: tuple[str, ...] = Field(default=(), max_length=100)
    symbols: tuple[str, ...] = Field(default=(), max_length=100)
    strategy: RetrievalStrategy = "reranked"
    limit: int = Field(default=8, ge=1, le=20)


class ContextEvidence(RetrievalContract):
    agent: ReviewAgent | None = None
    unit_keys: tuple[str, ...] = ()
    reference_id: str
    chunk_id: str
    index_id: str
    head_sha: str
    file: str
    blob_sha: str
    symbol: str
    start_line: int
    end_line: int
    content: str
    content_hash: str
    routes: tuple[RetrievalRoute, ...]
    route_scores: dict[str, float] = Field(default_factory=dict)
    route_ranks: dict[str, int] = Field(default_factory=dict)
    rank: int
    fused_rank: int
    fusion_score: float
    rerank_score: float | None = None
    selected: bool = False


    @model_validator(mode="after")
    def validate_evidence(self):
        if self.start_line < 1 or self.end_line < self.start_line:
            raise ValueError("关联证据行号无效")
        if sha256(self.content.encode()).hexdigest() != self.content_hash:
            raise ValueError("关联证据内容哈希不一致")
        if self.reference_id != stable_key(self.index_id, self.chunk_id):
            raise ValueError("关联证据身份不一致")
        normalize_repository_path(self.file)
        normalize_sha(self.head_sha)
        normalize_sha(self.blob_sha)
        return self


class RouteMetric(RetrievalContract):
    route: RetrievalRoute
    candidate_count: int
    duration_ms: int


class RetrievalTrace(RetrievalContract):
    agent: ReviewAgent | None = None
    plan_fingerprint: str | None = None
    id: str
    index_id: str
    query: str
    strategy: RetrievalStrategy
    requested_strategy: RetrievalStrategy | None = None
    strategies_used: tuple[RetrievalStrategy, ...] = ()
    queries: tuple[str, ...] = ()
    covered_units: int = 0
    total_units: int = 0
    model_requests: int = 0
    rerank_cache_hit: bool = False
    vector_search_mode: str = "unused"
    candidates: tuple[ContextEvidence, ...]
    routes: tuple[RouteMetric, ...]
    duration_ms: int
    embedding_ms: int = 0
    rerank_ms: int = 0
    query_cache_hit: bool = False
    input_tokens: int | None = None
    rerank_tokens: int | None = None
    warnings: tuple[str, ...] = ()

    @property
    def selected(self) -> tuple[ContextEvidence, ...]:
        return tuple(item for item in self.candidates if item.selected)


class IndexView(RetrievalContract):
    id: str
    parser_version: str = "java-mybatis-v1"
    repository: str
    repository_id: int
    installation_id: int
    head_sha: str
    status: str
    lexical_ready: bool = False
    vector_status: str = "pending"
    vector_count: int = 0
    vector_error: str | None = None
    embedding_model: str
    dimensions: int
    parsed_files: int = 0
    reused_files: int = 0
    file_count: int
    chunk_count: int
    relation_count: int
    embedded_count: int
    reused_count: int
    duration_ms: int | None
    parse_error_files: tuple[str, ...] = ()
    error: str | None = None
    preparation_started_at: str | None = None
    created_at: str
    completed_at: str | None = None


class IndexTarget(RetrievalContract):
    review_run_id: str
    repository: str
    pull_request_number: int
    head_sha: str


class RetrievalOperations(RetrievalContract):
    pending_indexes: int = 0
    oldest_pending_seconds: float = 0
    available_indexes: int = 0
    partial_indexes: int = 0
    provider_busy: bool = False
    circuit_open: bool = False


class RetrievalEvaluationCase(RetrievalContract):
    id: str
    query: str
    relevant_symbols: tuple[str, ...] = Field(min_length=1)
    seed_files: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    split: Literal["development", "validation"] = "validation"


class StrategyEvaluation(RetrievalContract):
    strategy: RetrievalStrategy
    sample_count: int
    recall_at_k: float
    mrr: float
    k: int
    median_duration_ms: float
    p95_duration_ms: float
    cases: tuple[dict[str, object], ...]


class RetrievalEvaluationReport(RetrievalContract):
    id: str
    index_id: str
    dataset_version: str
    annotation_source: AnnotationSource
    embedding_model: str
    rerank_model: str
    generated_at: str
    strategies: tuple[StrategyEvaluation, ...]
    query_cache_mode: str = "shared_warm"
    lexical_cache_mode: str = "unknown"
    vector_search_mode: str = "exact_snapshot"
    real_review_accuracy: float | None = None


class SearchHistoryItem(RetrievalContract):
    id: str
    index_id: str
    repository: str
    head_sha: str
    query: str
    strategy: RetrievalStrategy
    requested_strategy: RetrievalStrategy
    duration_ms: int
    created_at: str


class RetrievalComparisonRequest(RetrievalContract):
    query: str = Field(min_length=1, max_length=4000)
    relevant_symbols: tuple[str, ...] = Field(min_length=1, max_length=50)
    strategies: tuple[RetrievalStrategy, ...] = ("bm25", "lexical_relations")
    k: int = Field(default=8, ge=1, le=20)
