"""为管理列表的游标翻页和审查任务搜索补充索引。"""

from alembic import op

revision = "20260912_0051"
down_revision = "20260910_0050"
branch_labels = None
depends_on = None

PAGE_INDEXES = (
    ("outbox_events", "ix_outbox_events_aggregate_occurred_id", ["aggregate_type", "aggregate_id", "occurred_at", "id"]),
    ("code_indexes", "ix_code_indexes_created_id", ["created_at", "id"]),
    ("retrieval_evaluations", "ix_retrieval_evaluations_created_id", ["created_at", "id"]),
    ("review_runs", "ix_review_runs_status_created_id", ["execution_status", "created_at", "id"]),
    ("review_runs", "ix_review_runs_pr_number", ["pull_request_number"]),
)
SEARCH_FIELDS = (
    ("review_runs", ("repository", "head_sha")),
    ("pull_request_versions", ("title", "author_login", "head_ref", "base_ref", "head_repository", "base_repository")),
)


def upgrade() -> None:
    for table, name, columns in PAGE_INDEXES:
        op.create_index(name, table, columns)
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        for table, columns in SEARCH_FIELDS:
            for column in columns:
                op.create_index(f"ix_{table}_{column}_trgm", table, [column],
                    postgresql_using="gin", postgresql_ops={column: "gin_trgm_ops"})


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table, columns in SEARCH_FIELDS:
            for column in columns:
                op.drop_index(f"ix_{table}_{column}_trgm", table_name=table)
    for table, name, _ in reversed(PAGE_INDEXES):
        op.drop_index(name, table_name=table)
