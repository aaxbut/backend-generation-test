from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionFactory
from app.models import Attempt, AttemptStatus
from app.services import attempts as attempts_service

log = logging.getLogger(__name__)


async def _claim_overdue(session: AsyncSession, limit: int) -> list[tuple]:
    """
    Забрать просроченные pending-попытки без задвоения между воркерами.
    SKIP LOCKED: второй воркер не блокируется, а пропускает залоченные строки.
    Блокировка живёт до конца транзакции — поэтому claim и обработка
    должны быть в одной транзакции на попытку, иначе между claim и
    close_timed_out_attempt блокировка снимется и второй воркер возьмёт ту же.
    """
    now = datetime.now(UTC)
    rows = (
        (
            await session.execute(
                select(Attempt.id)
                .where(
                    Attempt.status == AttemptStatus.pending,
                    Attempt.deadline_at <= now,
                )
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def process_one_batch(limit: int = 50) -> int:
    from app.models import Attempt, AttemptStatus
    from app.services import providers_client
    from app.services.attempts import _timed_out_in_tx

    processed = 0
    new_ids: list[uuid.UUID] = []

    async with SessionFactory() as session:
        async with session.begin():
            now = datetime.now(UTC)
            attempts_rows = (
                (
                    await session.execute(
                        select(Attempt)
                        .where(
                            Attempt.status == AttemptStatus.pending,
                            Attempt.deadline_at <= now,
                        )
                        .with_for_update(skip_locked=True)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            for a in attempts_rows:
                new_id = await _timed_out_in_tx(session, attempt=a)
                if new_id is not None:
                    new_ids.append(new_id)
                processed += 1

    # HTTP вне транзакции
    if new_ids:
        async with SessionFactory() as s:
            for nid in new_ids:
                na = await s.get(Attempt, nid)
                await providers_client.send_attempt(
                    task_id=na.task_id, attempt_id=na.id, provider=na.provider
                )
    return processed


async def _close_timed_out_in_tx(session: AsyncSession, attempt_id) -> None:
    """
    Тело close_timed_out_attempt БЕЗ session.begin() — т.к. мы уже в транзакции
    сторожа, которая держит лок на строке. CAS pending→timed_out всё равно
    нужен: лок есть, но статус мог измениться до лока (маловероятно,
    но CAS — вторая страховка).
    """
    from datetime import datetime

    from sqlalchemy import update

    from app.models import Attempt, AttemptStatus, ErrorType

    attempt = (await session.execute(select(Attempt).where(Attempt.id == attempt_id))).scalar_one()
    task = await attempts_service._load_task(session, attempt.task_id)

    now = datetime.now(UTC)
    res = await session.execute(
        update(Attempt)
        .where(Attempt.id == attempt_id, Attempt.status == AttemptStatus.pending)
        .values(
            status=AttemptStatus.timed_out,
            error_type=ErrorType.technical,
            finished_at=now,
        )
        .returning(Attempt.id)
    )
    if res.scalar_one_or_none() is None:
        return
    from app.models import TaskStatus

    if task.status != TaskStatus.running:
        return
    await attempts_service._advance_or_refund(session, task, error_type=ErrorType.technical)


async def run_forever(interval_seconds: float = 1.0) -> None:
    """Запускается в каждом воркере при старте приложения."""
    log.info("watchdog started, interval=%ss", interval_seconds)
    while True:
        try:
            n = await process_one_batch()
            if n:
                log.info("watchdog processed %d attempts", n)
        except Exception:
            log.exception("watchdog tick failed")
        await asyncio.sleep(interval_seconds)
