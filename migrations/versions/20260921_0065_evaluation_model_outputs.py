"""为显式评测运行留存有界供应商输出和独立来源元数据。"""

import sqlalchemy as sa
from alembic import op

revision = "20260921_0065"
down_revision = "20260921_0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("review_runs", sa.Column("capture_model_outputs", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("review_runs", sa.Column("evaluation_output_bytes", sa.Integer(), nullable=False, server_default="0"))
    op.create_table("evaluation_model_outputs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("review_run_id", sa.String(36), nullable=False),
        sa.Column("review_plan_id", sa.String(36), nullable=False),
        sa.Column("installation_id", sa.BigInteger(), nullable=False),
        sa.Column("repository", sa.String(255), nullable=False),
        sa.Column("repository_key", sa.String(255), nullable=False),
        sa.Column("head_sha", sa.String(64), nullable=False),
        sa.Column("profile_id", sa.String(36)),
        sa.Column("agent", sa.String(32), nullable=False),
        sa.Column("batch_number", sa.Integer()),
        sa.Column("split_depth", sa.Integer(), nullable=False),
        sa.Column("request_sequence", sa.BigInteger(), nullable=False),
        sa.Column("attempt_kind", sa.String(24), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("api_protocol", sa.String(32), nullable=False),
        sa.Column("prompt_content_sha256", sa.String(64), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("application_revision", sa.String(40)),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("output_format", sa.String(40), nullable=False),
        sa.Column("output_text", sa.Text()),
        sa.Column("output_sha256", sa.String(64)),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(80)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('pending','captured','parse_failed','transport_failed','oversized','run_limit','missing','expired')", name="status_value"),
        sa.CheckConstraint("byte_size BETWEEN 0 AND 262144", name="byte_size_bounded"),
    )
    op.create_index("ix_evaluation_model_outputs_run_created", "evaluation_model_outputs", ["review_run_id", "created_at", "id"])
    op.create_index("ix_evaluation_model_outputs_expiry", "evaluation_model_outputs", ["expires_at", "status", "id"])


def downgrade() -> None:
    op.drop_table("evaluation_model_outputs")
    op.drop_column("review_runs", "evaluation_output_bytes")
    op.drop_column("review_runs", "capture_model_outputs")
