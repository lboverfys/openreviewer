"""保存不可变静态佐证与跨提交审查复用索引。"""

import sqlalchemy as sa
from alembic import op

revision = "20260913_0058"
down_revision = "20260913_0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("static_analysis_reports",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("review_run_id", sa.String(36), sa.ForeignKey("review_runs.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("tool", sa.String(50), nullable=False),
        sa.Column("tool_version", sa.String(100), nullable=False),
        sa.Column("head_sha", sa.String(64), nullable=False),
        sa.Column("base_sha", sa.String(64)),
        sa.Column("report_hash", sa.String(64), nullable=False),
        sa.Column("finding_count", sa.Integer(), nullable=False),
        sa.Column("new_count", sa.Integer(), nullable=False),
        sa.Column("existing_count", sa.Integer(), nullable=False),
        sa.Column("unknown_count", sa.Integer(), nullable=False),
        sa.Column("imported_by", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table("static_analysis_findings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("report_id", sa.String(36), sa.ForeignKey("static_analysis_reports.id", ondelete="CASCADE"), nullable=False),
        sa.Column("rule_id", sa.String(300), nullable=False),
        sa.Column("file", sa.String(1024), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("level", sa.String(20), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("baseline_state", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_static_findings_report_page", "static_analysis_findings", ["report_id", "created_at", "id"])
    op.create_index("ix_review_findings_run_file_line", "review_findings", ["review_run_id", "location_file", "location_start_line"])
    op.create_table("review_reuse_entries",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("source_run_id", sa.String(36), sa.ForeignKey("review_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("head_sha", sa.String(64), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_review_reuse_source", "review_reuse_entries", ["source_run_id"])


def downgrade() -> None:
    op.drop_table("review_reuse_entries")
    op.drop_index("ix_review_findings_run_file_line", table_name="review_findings")
    op.drop_table("static_analysis_findings")
    op.drop_table("static_analysis_reports")
