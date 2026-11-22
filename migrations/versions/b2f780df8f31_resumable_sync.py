"""Remove the legacy grade preference and persist pagination progress."""

import sqlalchemy as sa
from alembic import op

revision = "b2f780df8f31"
down_revision = "f2b55311b3a2"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_context().dialect.name == "postgresql":
        op.execute(
            "UPDATE notification_rules SET data = CAST(CAST(data AS jsonb) - 'score_in_email' AS json) "
            "WHERE json_typeof(data) = 'object' AND json_extract_path(data, 'score_in_email') IS NOT NULL"
        )
    else:
        op.execute(
            "UPDATE notification_rules SET data = json_remove(data, '$.score_in_email') "
            "WHERE json_type(data, '$.score_in_email') IS NOT NULL"
        )
    # 旧版只有全局首次摘要标记；已建立作业基线的课程视为已发过摘要，升级后不再补发历史摘要。
    op.execute(
        "INSERT INTO health (name, status, updated_at, details) "
        "SELECT DISTINCT 'initial_digest:' || s.course_id, 'created', h.updated_at, h.details "
        "FROM sync_scopes s CROSS JOIN health h "
        "WHERE h.name = 'initial_digest' AND h.status = 'created' "
        "AND s.kind = 'assignment' AND s.baseline AND s.course_id <> ''"
    )
    op.execute("DELETE FROM health WHERE name = 'initial_digest'")
    op.add_column("resources", sa.Column("last_listed", sa.DateTime(timezone=True)))
    op.add_column("sync_scopes", sa.Column("scan_started", sa.DateTime(timezone=True)))
    op.add_column("sync_scopes", sa.Column("scan_urls", sa.JSON(none_as_null=True)))


def downgrade():
    # 无法还原已删除的旧规则值。
    op.drop_column("sync_scopes", "scan_urls")
    op.drop_column("sync_scopes", "scan_started")
    op.drop_column("resources", "last_listed")
