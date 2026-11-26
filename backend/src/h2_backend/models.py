from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Task(Base):
    __tablename__ = "inspection_tasks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="pending_dispatch")
    progress: Mapped[float] = mapped_column(Float, default=0)
    points: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    review_note: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    simulated: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Outbox(Base):
    __tablename__ = "inspection_outbox"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("inspection_tasks.id"), unique=True)
    expires_at_ms: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class StoredFile(Base):
    __tablename__ = "inspection_files"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("inspection_tasks.id"), index=True)
    path: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(String(250))
    media_type: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(30))
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Sample(Base):
    __tablename__ = "inspection_samples"
    __table_args__ = (UniqueConstraint("task_id", "point_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("inspection_tasks.id"), index=True)
    point_id: Mapped[str] = mapped_column(String(100))
    label: Mapped[str] = mapped_column(String(200))
    device_id: Mapped[str] = mapped_column(String(100), default="")
    temperature_c: Mapped[float] = mapped_column(Float)
    humidity_percent: Mapped[float] = mapped_column(Float)
    captured_at: Mapped[str] = mapped_column(String(60))
    image_file_id: Mapped[str | None] = mapped_column(ForeignKey("inspection_files.id"), nullable=True)
    simulated: Mapped[bool] = mapped_column(default=True)


class ReportJob(Base):
    __tablename__ = "inspection_report_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("inspection_tasks.id"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Review(Base):
    __tablename__ = "inspection_reviews"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("inspection_tasks.id"), index=True)
    note: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DemoRoute(Base):
    """巡检演示的运动路线：步骤列表以 JSON 存储（参照 Task.points）。

    单个步骤形如 {"type": "forward|turn|wait", "value": 米/度/秒, "read_aloud": bool}，
    read_aloud 仅对停留步有意义（停留期间朗读视觉识别结果）。
    """
    __tablename__ = "demo_routes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AppCredential(Base):
    """上位机登录凭据：单行（id=1）存储真机模式登录门的密码哈希与盐。

    仅作为操作闸门（方案 A）：其余 API 不做拦截，真实运动安全仍由
    遥控租约独占与现场规程保障。首次验证时自动播种默认密码。
    """
    __tablename__ = "app_credential"
    id: Mapped[int] = mapped_column(primary_key=True)
    salt: Mapped[str] = mapped_column(String(32))
    password_hash: Mapped[str] = mapped_column(String(64))
