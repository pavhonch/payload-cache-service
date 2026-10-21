"""Create transformation cache and request index."""

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "transformations",
        sa.Column("transformer_version", sa.String(64), primary_key=True),
        sa.Column("source_hash", sa.LargeBinary(), primary_key=True),
        sa.Column("source", sa.LargeBinary(), nullable=False),
        sa.Column("result", sa.LargeBinary(), nullable=False),
        sa.CheckConstraint("octet_length(source_hash) = 32", name="ck_source_hash_length"),
    )
    op.create_table(
        "request_payloads",
        sa.Column("request_hash", sa.LargeBinary(), primary_key=True),
        sa.Column("payload_id", sa.String(64), nullable=False),
        sa.CheckConstraint("octet_length(request_hash) = 32", name="ck_request_hash_length"),
        sa.CheckConstraint("payload_id ~ '^[0-9a-f]{64}$'", name="ck_payload_id_format"),
    )


def downgrade() -> None:
    op.drop_table("request_payloads")
    op.drop_table("transformations")
