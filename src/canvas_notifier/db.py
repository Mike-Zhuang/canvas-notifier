from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import JSON, DateTime, Integer, String, Text, event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from canvas_notifier.domain.time import now_utc


class UTCDateTime(TypeDecorator):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is None:
            raise ValueError("Naive timestamp rejected")
        return value.astimezone(UTC) if value is not None else None

    def process_result_value(self, value, dialect):
        return value.replace(tzinfo=UTC) if value and value.tzinfo is None else value


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    origin: Mapped[str] = mapped_column(String(255))
    user_id: Mapped[str] = mapped_column(String(80))
    auth: Mapped[dict] = mapped_column(JSON, default=dict)


class Resource(Base):
    __tablename__ = "resources"
    key: Mapped[str] = mapped_column(String(400), primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    external_id: Mapped[str] = mapped_column(String(160))
    course_id: Mapped[str] = mapped_column(String(80), default="", index=True)
    scope: Mapped[str] = mapped_column(String(400), index=True)
    data: Mapped[dict] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(default=1)
    first_seen: Mapped[datetime] = mapped_column(UTCDateTime, default=now_utc)
    last_seen: Mapped[datetime] = mapped_column(UTCDateTime, default=now_utc)
    missing_count: Mapped[int] = mapped_column(default=0)
    availability: Mapped[str] = mapped_column(String(40), default="visible")
    local: Mapped[dict] = mapped_column(JSON, default=dict)


class Scope(Base):
    __tablename__ = "sync_scopes"
    key: Mapped[str] = mapped_column(String(400), primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))
    course_id: Mapped[str] = mapped_column(String(80), default="")
    baseline: Mapped[bool] = mapped_column(default=False)
    pages: Mapped[int] = mapped_column(default=0)
    count: Mapped[int] = mapped_column(default=0)
    complete: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(String(80), default="never")
    cursor: Mapped[str | None] = mapped_column(Text)
    last_attempt: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_success: Mapped[datetime | None] = mapped_column(UTCDateTime)
    next_attempt: Mapped[datetime | None] = mapped_column(UTCDateTime)
    verification: Mapped[str] = mapped_column(String(80), default="tested_with_synthetic_fixture")


class SyncRun(Base):
    __tablename__ = "sync_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_utc)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(String(80), default="running")
    summary: Mapped[dict] = mapped_column(JSON, default=dict)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    unique_key: Mapped[str] = mapped_column(String(500), unique=True)
    resource_key: Mapped[str] = mapped_column(String(400), index=True)
    kind: Mapped[str] = mapped_column(String(80))
    changes: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_utc)
    disposition: Mapped[str] = mapped_column(String(80), default="generated")


class Rule(Base):
    __tablename__ = "notification_rules"
    scope: Mapped[str] = mapped_column(String(400), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(default=1)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_utc)


class Reminder(Base):
    __tablename__ = "reminder_jobs"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    resource_key: Mapped[str] = mapped_column(String(400), index=True)
    boundary_type: Mapped[str] = mapped_column(String(20))
    boundary_at: Mapped[datetime] = mapped_column(UTCDateTime)
    scheduled_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    offset_seconds: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(40), default="pending", index=True)
    reason: Mapped[str] = mapped_column(String(200), default="")
    rule_fingerprint: Mapped[str] = mapped_column(String(64))


class Delivery(Base):
    __tablename__ = "outbox"
    id: Mapped[int] = mapped_column(primary_key=True)
    unique_key: Mapped[str] = mapped_column(String(600), unique=True)
    resource_key: Mapped[str] = mapped_column(String(400), default="", index=True)
    recipient: Mapped[str] = mapped_column(String(254))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(40), default="pending", index=True)
    reason: Mapped[str] = mapped_column(String(200), default="")
    attempts: Mapped[int] = mapped_column(default=0)
    available_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_utc, index=True)
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    lease_owner: Mapped[str | None] = mapped_column(String(80))
    message_id: Mapped[str] = mapped_column(String(180))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_utc)
    accepted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Lease(Base):
    __tablename__ = "leases"
    name: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner: Mapped[str] = mapped_column(String(80))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)


class Health(Base):
    __tablename__ = "health"
    name: Mapped[str] = mapped_column(String(80), primary_key=True)
    status: Mapped[str] = mapped_column(String(80))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_utc)
    details: Mapped[dict] = mapped_column(JSON, default=dict)


def database(url: str):
    if url.startswith("sqlite"):
        filename = make_url(url).database
        if filename and filename != ":memory:":
            Path(filename).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    engine = create_async_engine(url, pool_pre_ping=True)
    if url.startswith("sqlite"):

        def configure_sqlite(connection, record):
            cursor = connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=FULL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        event.listen(engine.sync_engine, "connect", configure_sqlite)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def create_schema(engine):
    # 测试与临时 dry-run 数据库使用；正式数据库由 Alembic 迁移。
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
