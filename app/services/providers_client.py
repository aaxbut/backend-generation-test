from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid

import httpx
from sqlalchemy import select, update

from app.config import settings
from app.db import SessionFactory
from app.models import (
    Attempt,
    AttemptStatus,
    ErrorType,
    Task,
)
from app.services import attempts as attempts_service

log = logging.getLogger(__name__)


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def _post_task(
    *,
    task: Task,
    attempt: Attempt,
    provider: str,
    behavior: dict | None,
) -> tuple[str | None, bool]:
    """
    Возвращает (external_id, sync_error).
    HTTP — вне транзакции.
    """
    payload = {
        "task_id": str(task.id),
        "attempt_id": str(attempt.id),
        "prompt": task.prompt,
        "ref_images": task.ref_images,
        "model_slug": task.model_slug,
    }
    if behavior:
        payload["behavior"] = behavior
    raw = json.dumps(payload, separators=(",", ":")).encode()

    try:
        async with httpx.AsyncClient(timeout=settings.provider_sync_timeout) as client:
            resp = await client.post(
                f"{settings.provider_url(provider)}/tasks",
                content=raw,
                headers={
                    "content-type": "application/json",
                    "X-Signature": _sign(settings.provider_secret(provider), raw),
                },
            )
        if resp.status_code == 202:
            ext = resp.json().get("external_id")
            return ext, ext is None
        # 5xx и прочие не-202 — синхронная ошибка (правило 3).
        return None, True
    except (httpx.TimeoutException, httpx.TransportError) as e:
        log.warning("provider %s sync error: %r", provider, e)
        return None, True


async def send_attempt(
    *,
    task_id: uuid.UUID,
    attempt_id: uuid.UUID,
    provider: str,
    behavior: dict | None = None,
) -> None:
    """
    Отправить попытку провайдеру. Вызывается ПОСЛЕ коммита транзакции,
    создавшей Attempt(pending). HTTP — вне транзакции.
    """
    # 1. Читаем task/attempt для payload (короткая транзакция, можно без begin()).
    async with SessionFactory() as session:
        async with session.begin():
            task = (await session.execute(select(Task).where(Task.id == task_id))).scalar_one()
            attempt = (
                await session.execute(select(Attempt).where(Attempt.id == attempt_id))
            ).scalar_one()

    # 2. HTTP
    external_id, sync_error = await _post_task(
        task=task, attempt=attempt, provider=provider, behavior=behavior
    )

    if sync_error:
        # Правило 3: синхронная ошибка == failed(technical) → следующая нога/возврат.
        async with SessionFactory() as s:
            await attempts_service.fail_attempt_and_advance(
                s, attempt_id=attempt_id, error_type=ErrorType.technical
            )
        return

    # 3. Сохраняем external_id (только если попытка ещё pending и external_id пуст).
    async with SessionFactory() as s:
        async with s.begin():
            await s.execute(
                update(Attempt)
                .where(
                    Attempt.id == attempt_id,
                    Attempt.status == AttemptStatus.pending,
                    Attempt.external_id.is_(None),
                )
                .values(external_id=external_id)
            )


async def dispatch_first_attempt(
    *,
    task_id: uuid.UUID,
    attempt_id: uuid.UUID,
    provider: str,
    behavior: dict | None = None,
) -> None:
    """Обёртка для совместимости: первая нога — это send_attempt с behavior."""
    await send_attempt(task_id=task_id, attempt_id=attempt_id, provider=provider, behavior=behavior)
