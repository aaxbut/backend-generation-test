from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ---------- Перечисления ----------


class TaskStatus(str, enum.Enum):
    running = "running"  # есть хотя бы одна активная попытка
    completed = "completed"  # успешно завершена
    failed = "failed"  # ноги кончились / payload / техническая смерть


class AttemptStatus(str, enum.Enum):
    pending = "pending"
    completed = "completed"
    failed = "failed"
    timed_out = "timed_out"  # сторож сдался; окончательный статус (правило 6)


class ErrorType(str, enum.Enum):
    technical = "technical"
    moderation = "moderation"
    payload = "payload"


class LedgerKind(str, enum.Enum):
    charge = "charge"  # списание за генерацию
    refund = "refund"  # возврат при failed
    topup = "topup"  # оплата
    late_ignored = "late_ignored"  # правило 6: поздний completed, деньги не двигаем


# ---------- Пользователь и журнал ----------


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    balance: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LedgerEntry(Base):
    """
    Журнал. Инвариант (правило 8): users.balance == SUM(ledger.delta) по user_id.
    Пишется всегда в той же транзакции, что и изменение balance.
    """

    __tablename__ = "ledger"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[LedgerKind] = mapped_column(Enum(LedgerKind, name="ledger_kind"), nullable=False)
    delta: Mapped[int] = mapped_column(BigInteger, nullable=False)  # +topup, -charge, +refund
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    payment_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # Страховка «ровно один раз» для возврата и списания на задачу:
        # не более одной строки каждого вида на task_id.
        Index(
            "uq_ledger_charge_per_task",
            "task_id",
            unique=True,
            postgresql_where=(kind == LedgerKind.charge),
        ),
        Index(
            "uq_ledger_refund_per_task",
            "task_id",
            unique=True,
            postgresql_where=(kind == LedgerKind.refund),
        ),
        Index(
            "uq_ledger_late_ignored_per_task",
            "task_id",
            unique=True,
            postgresql_where=(kind == LedgerKind.late_ignored),
        ),
        # Одна оплата — одна строка topup, даже если вебхук пришёл 20 раз.
        Index(
            "uq_ledger_topup_per_payment",
            "payment_id",
            unique=True,
            postgresql_where=(kind == LedgerKind.topup),
        ),
        Index("ix_ledger_user_created", "user_id", "created_at"),
    )


# ---------- Оплаты ----------


class Payment(Base):
    """
    Дедупликация вебхуков оплаты: payment_id — PK.
    INSERT ... ON CONFLICT DO NOTHING RETURNING решает «ровно один раз».
    """

    __tablename__ = "payments"

    payment_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)  # "paid" и т.п.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ---------- Каталог моделей и ног ----------


class Model(Base):
    __tablename__ = "models"

    slug: Mapped[str] = mapped_column(String(64), primary_key=True)
    price: Mapped[int] = mapped_column(Integer, nullable=False)  # токенов за генерацию

    legs: Mapped[list[Leg]] = relationship(
        back_populates="model", order_by="Leg.leg_index", cascade="all, delete-orphan"
    )


class Leg(Base):
    __tablename__ = "legs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_slug: Mapped[str] = mapped_column(
        String(64), ForeignKey("models.slug", ondelete="CASCADE"), nullable=False
    )
    leg_index: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)  # alpha|beta
    max_refs: Mapped[int] = mapped_column(Integer, nullable=False)
    skip_on_moderation: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="false"
    )

    model: Mapped[Model] = relationship(back_populates="legs")

    __table_args__ = (UniqueConstraint("model_slug", "leg_index", name="uq_leg_model_index"),)


# ---------- Задачи и попытки ----------


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    model_slug: Mapped[str] = mapped_column(
        String(64), ForeignKey("models.slug", ondelete="RESTRICT"), nullable=False
    )
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    ref_images: Mapped[int] = mapped_column(Integer, nullable=False)  # храним число, не сами файлы
    price: Mapped[int] = mapped_column(
        Integer, nullable=False
    )  # зафиксировали цену на момент создания

    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, name="task_status"), nullable=False, default=TaskStatus.running
    )
    current_leg_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    attempts: Mapped[list[Attempt]] = relationship(
        back_populates="task", order_by="Attempt.leg_index", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # Для проверки лимита 3 активных задач пользователя (правило «429»).
        Index(
            "ix_tasks_user_active",
            "user_id",
            postgresql_where=(status == TaskStatus.running),
        ),
    )


class Attempt(Base):
    __tablename__ = "attempts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    leg_index: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)

    status: Mapped[AttemptStatus] = mapped_column(
        Enum(AttemptStatus, name="attempt_status"), nullable=False, default=AttemptStatus.pending
    )
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_type: Mapped[ErrorType | None] = mapped_column(
        Enum(ErrorType, name="error_type"), nullable=True
    )
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    result_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    task: Mapped[Task] = relationship(back_populates="attempts")

    __table_args__ = (
        UniqueConstraint("task_id", "leg_index", name="uq_attempt_task_leg"),
        # Вебхук ищет попытку по external_id + provider.
        Index("ix_attempt_external", "provider", "external_id", unique=False),
        # Сторож ищет просроченные pending.
        Index(
            "ix_attempt_pending_deadline",
            "deadline_at",
            postgresql_where=(status == AttemptStatus.pending),
        ),
    )
