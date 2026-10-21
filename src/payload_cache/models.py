from sqlalchemy import CheckConstraint, LargeBinary, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Transformation(Base):
    __tablename__ = "transformations"
    __table_args__ = (
        CheckConstraint("octet_length(source_hash) = 32", name="ck_source_hash_length"),
    )

    transformer_version: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_hash: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    source: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    result: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)


class RequestPayload(Base):
    __tablename__ = "request_payloads"
    __table_args__ = (
        CheckConstraint("octet_length(request_hash) = 32", name="ck_request_hash_length"),
        CheckConstraint("payload_id ~ '^[0-9a-f]{64}$'", name="ck_payload_id_format"),
    )

    request_hash: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    payload_id: Mapped[str] = mapped_column(String(64), nullable=False)
