"""增加独立评测集、PR 样本和结构化观察快照。"""

import sqlalchemy as sa
from alembic import op

revision = "20260912_0053"
down_revision = "20260912_0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evaluation_datasets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("installation_id", sa.BigInteger(), nullable=False),
        sa.Column("repository_id", sa.BigInteger(), nullable=False),
        sa.Column("repository", sa.String(255), nullable=False),
        sa.Column("repository_key", sa.String(255), nullable=False),
        sa.Column("request_key", sa.String(64), nullable=False, unique=True),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("case_count", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision > 0", name="revision_positive"),
        sa.CheckConstraint("case_count BETWEEN 0 AND 200", name="case_count_bounded"),
    )
    op.create_index("ix_evaluation_datasets_archive_created", "evaluation_datasets",
                    ["archived_at", "created_at", "id"])
    op.create_index("ix_evaluation_datasets_repository_created", "evaluation_datasets",
                    ["repository_key", "created_at", "id"])
    op.create_table(
        "evaluation_cases",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("dataset_id", sa.String(36),
                  sa.ForeignKey("evaluation_datasets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("pull_request_number", sa.Integer(), nullable=False),
        sa.Column("head_sha", sa.String(64), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("split", sa.String(16), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("reference_defects", sa.JSON()),
        sa.Column("reference_reviews", sa.JSON(), nullable=False),
        sa.Column("reference_status", sa.String(16), nullable=False),
        sa.Column("reference_count", sa.Integer()),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("dataset_id", "pull_request_number"),
        sa.CheckConstraint("revision > 0", name="revision_positive"),
        sa.CheckConstraint("split IN ('tuning','validation')", name="split_value"),
        sa.CheckConstraint("kind IN ('normal','known_defect','cross_file')", name="kind_value"),
    )
    op.create_index("ix_evaluation_cases_dataset_created", "evaluation_cases",
                    ["dataset_id", "created_at", "id"])
    op.create_table(
        "evaluation_observations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("case_id", sa.String(36),
                  sa.ForeignKey("evaluation_cases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("variant", sa.String(16), nullable=False),
        sa.Column("source_run_id", sa.String(36), nullable=False),
        sa.Column("source_snapshot", sa.JSON(), nullable=False),
        sa.Column("snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("configuration_fingerprint", sa.String(64), nullable=False),
        sa.Column("model_label", sa.String(600), nullable=False),
        sa.Column("provenance_complete", sa.Boolean(), nullable=False),
        sa.Column("finding_count", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("model_duration_ms", sa.BigInteger(), nullable=False),
        sa.Column("turnaround_ms", sa.BigInteger(), nullable=False),
        sa.Column("estimated_cost_microusd", sa.BigInteger()),
        sa.Column("ballots", sa.JSON(), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("assessment_status", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("captured_by", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("case_id", "variant"),
        sa.CheckConstraint("variant IN ('baseline','candidate')", name="variant_value"),
        sa.CheckConstraint("revision > 0", name="revision_positive"),
        sa.CheckConstraint("finding_count BETWEEN 0 AND 500", name="finding_count_bounded"),
        sa.CheckConstraint(
            "assessment_status IN ('pending','partial','disputed','complete')",
            name="assessment_status_value",
        ),
    )


def downgrade() -> None:
    op.drop_table("evaluation_observations")
    op.drop_table("evaluation_cases")
    op.drop_table("evaluation_datasets")
