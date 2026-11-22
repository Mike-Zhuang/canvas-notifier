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
    op.add_column("resources", sa.Column("last_listed", sa.DateTime(timezone=True)))
    op.add_column("sync_scopes", sa.Column("scan_started", sa.DateTime(timezone=True)))
    op.add_column("sync_scopes", sa.Column("scan_urls", sa.JSON(none_as_null=True)))


def downgrade():
    # 无法还原已删除的旧规则值。
    op.drop_column("sync_scopes", "scan_urls")
    op.drop_column("sync_scopes", "scan_started")
    op.drop_column("resources", "last_listed")
