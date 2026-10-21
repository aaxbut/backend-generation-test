# app/services/attempts.py
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    Attempt,
    AttemptStatus,
    ErrorType,
    LedgerEntry,
    LedgerKind,
    Leg,
    Task,
    TaskStatus,
    User,
)

log = logging.getLogger(__name__)


class UnknownAttempt(Exception):
    """external_id не найден или найден не один раз."""


# ---------- чтение ----------


async def _load_task(session: AsyncSession, task_id: uuid.UUID) -> Task:
    t = (await session.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
    if t is None:
        raise LookupError(f"task {task_id} not found")
    return t


async def _resolve_attempt(
    session: AsyncSession,
    *,
    attempt_id: uuid.UUID | None = None,
    provider: str | None = None,
    external_id: str | None = None,
) -> Attempt:
    if attempt_id is not None:
        a = (
            await session.execute(select(Attempt).where(Attempt.id == attempt_id))
        ).scalar_one_or_none()
        if a is None:
            raise LookupError(f"attempt {attempt_id} not found")
        return a

    if provider is None or external_id is None:
        raise ValueError("either attempt_id or (provider, external_id) required")

    rows = (
        (
            await session.execute(
                select(Attempt).where(
                    Attempt.provider == provider,
                    Attempt.external_id == external_id,
                )
            )
        )
        .scalars()
        .all()
    )
    if len(rows) != 1:
        raise UnknownAttempt(
            f"attempt lookup provider={provider} external_id={external_id} returned {len(rows)}"
        )
    return rows[0]


async def _next_suitable_leg(
    session: AsyncSession,
    task: Task,
    *,
    error_type: ErrorType,
) -> Leg | None:
    if error_type == ErrorType.payload:
        return None
    legs = list(
        (
            await session.execute(
                select(Leg).where(Leg.model_slug == task.model_slug).order_by(Leg.leg_index)
            )
        )
        .scalars()
        .all()
    )
    current = task.current_leg_index if task.current_leg_index is not None else -1
    log.info(
        "next_leg: task=%s model=%s current=%s error=%s legs=%s",
        task.id,
        task.model_slug,
        current,
        error_type,
        [(l.leg_index, l.provider, l.max_refs, l.skip_on_moderation) for l in legs],
    )
    for leg in legs:
        if leg.leg_index <= current:
            continue
        if error_type == ErrorType.moderation and leg.skip_on_moderation:
            continue
        return leg
    return None


# ---------- возврат / продвижение ----------


async def _refund_once_in_tx(session: AsyncSession, task: Task) -> None:
    now = datetime.now(UTC)
    res = await session.execute(
        update(Task)
        .where(Task.id == task.id, Task.status == TaskStatus.running)
        .values(status=TaskStatus.failed, finished_at=now, current_leg_index=None)
        .returning(Task.id)
    )
    if res.scalar_one_or_none() is None:
        return
    await session.execute(
        update(User).where(User.id == task.user_id).values(balance=User.balance + task.price)
    )
    session.add(
        LedgerEntry(
            user_id=task.user_id,
            kind=LedgerKind.refund,
            delta=task.price,
            task_id=task.id,
        )
    )
    log.info("task %s failed, refunded %d", task.id, task.price)


async def _advance_or_refund(
    session: AsyncSession,
    task: Task,
    *,
    error_type: ErrorType,
) -> uuid.UUID | None:
    """
    Возвращает id новой Attempt(pending), если перешли на следующую ногу,
    или None, если сделали возврат.
    Вызывается только внутри открытой транзакции. HTTP снаружи.
    """
    nxt = await _next_suitable_leg(session, task, error_type=error_type)
    if nxt is None:
        await _refund_once_in_tx(session, task)
        return None
    task.current_leg_index = nxt.leg_index
    new_attempt = Attempt(
        task_id=task.id,
        leg_index=nxt.leg_index,
        provider=nxt.provider,
        status=AttemptStatus.pending,
        deadline_at=datetime.now(UTC) + timedelta(seconds=settings.provider_timeout),
    )
    session.add(new_attempt)
    await session.flush()
    return new_attempt.id


# ---------- in-tx ядра ----------


async def _complete_in_tx(
    session: AsyncSession,
    *,
    attempt: Attempt,
    result_url: str,
) -> None:
    task = await _load_task(session, attempt.task_id)
    now = datetime.now(UTC)
    res = await session.execute(
        update(Attempt)
        .where(Attempt.id == attempt.id, Attempt.status == AttemptStatus.pending)
        .values(status=AttemptStatus.completed, result_url=result_url, finished_at=now)
        .returning(Attempt.id)
    )
    if res.scalar_one_or_none() is None:
        if attempt.status == AttemptStatus.timed_out:
            await session.execute(
                pg_insert(LedgerEntry)
                .values(
                    user_id=task.user_id,
                    kind=LedgerKind.late_ignored,
                    delta=0,
                    task_id=task.id,
                )
                .on_conflict_do_nothing(
                    index_elements=[LedgerEntry.task_id],
                    index_where=(LedgerEntry.kind == LedgerKind.late_ignored),
                )
            )
            log.info("late completed ignored attempt=%s task=%s", attempt.id, task.id)
        return
    await session.execute(
        update(Task)
        .where(Task.id == task.id, Task.status == TaskStatus.running)
        .values(
            status=TaskStatus.completed,
            result_url=result_url,
            finished_at=now,
            current_leg_index=None,
        )
    )


async def _fail_in_tx(
    session: AsyncSession,
    *,
    attempt: Attempt,
    error_type: ErrorType,
) -> uuid.UUID | None:
    task = await _load_task(session, attempt.task_id)
    now = datetime.now(UTC)
    res = await session.execute(
        update(Attempt)
        .where(Attempt.id == attempt.id, Attempt.status == AttemptStatus.pending)
        .values(status=AttemptStatus.failed, error_type=error_type, finished_at=now)
        .returning(Attempt.id)
    )
    if res.scalar_one_or_none() is None:
        return None
    if task.status != TaskStatus.running:
        return None
    return await _advance_or_refund(session, task, error_type=error_type)


async def _timed_out_in_tx(
    session: AsyncSession,
    *,
    attempt: Attempt,
) -> uuid.UUID | None:
    task = await _load_task(session, attempt.task_id)
    now = datetime.now(UTC)
    res = await session.execute(
        update(Attempt)
        .where(Attempt.id == attempt.id, Attempt.status == AttemptStatus.pending)
        .values(
            status=AttemptStatus.timed_out,
            error_type=ErrorType.technical,
            finished_at=now,
        )
        .returning(Attempt.id)
    )
    if res.scalar_one_or_none() is None:
        return None
    if task.status != TaskStatus.running:
        return None
    return await _advance_or_refund(session, task, error_type=ErrorType.technical)


# ---------- публичные обёртки ----------


async def complete_attempt(
    session: AsyncSession,
    *,
    attempt_id: uuid.UUID | None = None,
    provider: str | None = None,
    external_id: str | None = None,
    result_url: str,
) -> None:
    async with session.begin():
        attempt = await _resolve_attempt(
            session, attempt_id=attempt_id, provider=provider, external_id=external_id
        )
        await _complete_in_tx(session, attempt=attempt, result_url=result_url)


async def fail_attempt_and_advance(
    session: AsyncSession,
    *,
    attempt_id: uuid.UUID | None = None,
    provider: str | None = None,
    external_id: str | None = None,
    error_type: ErrorType,
) -> None:
    new_attempt_id: uuid.UUID | None = None
    async with session.begin():
        attempt = await _resolve_attempt(
            session, attempt_id=attempt_id, provider=provider, external_id=external_id
        )
        new_attempt_id = await _fail_in_tx(session, attempt=attempt, error_type=error_type)

    if new_attempt_id is not None:
        # Перечитать новую попытку и отправить провайдеру (HTTP вне транзакции).
        from app.services import providers_client  # локально, чтобы не было цикла

        new_attempt = await session.get(Attempt, new_attempt_id)
        await providers_client.send_attempt(
            task_id=new_attempt.task_id,
            attempt_id=new_attempt.id,
            provider=new_attempt.provider,
        )


async def close_timed_out_attempt(
    session: AsyncSession,
    *,
    attempt_id: uuid.UUID | None = None,
    provider: str | None = None,
    external_id: str | None = None,
) -> None:
    async with session.begin():
        attempt = await _resolve_attempt(
            session, attempt_id=attempt_id, provider=provider, external_id=external_id
        )
        await _timed_out_in_tx(session, attempt=attempt)
