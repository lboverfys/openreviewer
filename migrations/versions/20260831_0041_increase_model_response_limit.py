"""提高模型响应保护上限到 16 MiB。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260831_0041"
down_revision: str | None = "20260831_0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """允许较大的结构化模型响应，并提升历史默认值。"""

    connection = op.get_bind()
    recreate = "always" if connection.dialect.name == "sqlite" else "auto"
    with op.batch_alter_table("ai_provider_configs", recreate=recreate) as batch:
        batch.drop_constraint("max_response_bytes_range", type_="check")
        batch.create_check_constraint(
            "max_response_bytes_range",
            "max_response_bytes BETWEEN 65536 AND 16777216",
        )
    # 2 MiB 是历史默认值，也是当前故障的直接触发点。只提升仍使用该
    # 默认值的记录，不覆盖管理员已经明确设置过的其他响应上限。响应上限
    # 属于连接测试指纹；迁移后清掉旧测试结果，避免管理界面显示“已测试”
    # 但重新激活时因指纹不匹配被拒绝。
    op.execute(
        sa.text(
            "UPDATE ai_provider_configs "
            "SET max_response_bytes = 16777216, "
            "test_status = NULL, "
            "tested_configuration_fingerprint = NULL, "
            "tested_at = NULL "
            "WHERE max_response_bytes = 2097152"
        )
    )


def downgrade() -> None:
    """恢复 10 MiB 的历史模型响应上限。"""

    connection = op.get_bind()
    # 先收敛超出旧约束的值并清理对应测试状态，再重建约束；否则回滚会因
    # 现有 16 MiB 配置不满足 10 MiB 上限而失败，且会留下错误的配置指纹。
    connection.execute(
        sa.text(
            "UPDATE ai_provider_configs "
            "SET max_response_bytes = 10485760, "
            "test_status = NULL, "
            "tested_configuration_fingerprint = NULL, "
            "tested_at = NULL "
            "WHERE max_response_bytes > 10485760"
        )
    )
    recreate = "always" if connection.dialect.name == "sqlite" else "auto"
    with op.batch_alter_table("ai_provider_configs", recreate=recreate) as batch:
        batch.drop_constraint("max_response_bytes_range", type_="check")
        batch.create_check_constraint(
            "max_response_bytes_range",
            "max_response_bytes BETWEEN 65536 AND 10485760",
        )
