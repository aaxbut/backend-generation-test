from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import random
import uuid
from dataclasses import dataclass

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

SERVICE_WEBHOOK_URL = os.environ["SERVICE_WEBHOOK_URL"]
PROVIDER_SECRET = os.environ["PROVIDER_SECRET"]


@dataclass
class Behavior:
    """Настройки поведения, переопределяемые на один запрос через поле 'behavior' в теле."""

    fail_rate: float = 0.0
    fail_error_type: str = "technical"  # technical|moderation|payload
    sync_status: int = 202  # 202 — ок, 503 — синхронный отказ
    sync_timeout: bool = False  # True — «зависнуть» дольше PROVIDER_SYNC_TIMEOUT
    webhook_delay_min: float = 2.0
    webhook_delay_max: float = 5.0
    duplicate_webhook: bool = False  # прислать вебхук дважды
    concurrent_duplicate: bool = False  # два вебхука одновременно
    silent: bool = False  # не присылать вебхук вовсе
    late_completed: bool = False  # completed позже PROVIDER_TIMEOUT
    late_completed_after: float = 3.0  # через сколько секунд после дедлайна (в тестах)


def sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def build_app(provider_name: str) -> FastAPI:
    app = FastAPI()

    @app.post("/tasks")
    async def create_task(request: Request):
        raw = await request.body()
        sig = request.headers.get("x-signature")
        if not sig or not hmac.compare_digest(sign(PROVIDER_SECRET, raw), sig):
            return JSONResponse(status_code=401, content={"detail": "bad signature"})

        body = json.loads(raw)
        behavior_data = body.get("behavior") or {}
        b = Behavior(**{**Behavior().__dict__, **behavior_data})

        if b.sync_timeout:
            # «Зависаем» дольше синхронного таймаута клиента.
            await asyncio.sleep(30)
            return JSONResponse(status_code=504, content={"detail": "sync timeout"})

        if b.sync_status >= 500:
            return JSONResponse(status_code=b.sync_status, content={"detail": "sync fail"})

        external_id = f"{provider_name}-{uuid.uuid4().hex}"
        # Планируем вебхук в фоне; отвечаем 202 сразу.
        asyncio.create_task(_deliver_later(provider_name, external_id, b))
        return JSONResponse(status_code=202, content={"external_id": external_id})

    return app


async def _deliver_later(provider_name: str, external_id: str, b: Behavior) -> None:
    if b.silent:
        return

    # Основная доставка
    await asyncio.sleep(random.uniform(b.webhook_delay_min, b.webhook_delay_max))

    if b.late_completed:
        # Правило 6: completed после того, как сторож сдался.
        # Сначала молчим дольше PROVIDER_TIMEOUT, потом шлём completed.
        # В тестах PROVIDER_TIMEOUT маленький, поэтому ждём late_completed_after
        # сверх таймаута — его знает тест через env.
        await asyncio.sleep(b.late_completed_after)
        await _send(
            provider_name,
            {
                "external_id": external_id,
                "status": "completed",
                "result_url": f"https://fake/{external_id}",
            },
            b,
        )
        return

    failed = random.random() < b.fail_rate
    event = {
        "external_id": external_id,
        "status": "failed" if failed else "completed",
        "error_type": b.fail_error_type if failed else None,
        "result_url": None if failed else f"https://fake/{external_id}",
    }

    if b.concurrent_duplicate:
        await asyncio.gather(_send(provider_name, event, b), _send(provider_name, event, b))
    else:
        await _send(provider_name, event, b)
        if b.duplicate_webhook:
            await _send(provider_name, event, b)


async def _send(provider_name: str, event: dict, b: Behavior) -> None:
    raw = json.dumps(event, separators=(",", ":")).encode()
    headers = {
        "content-type": "application/json",
        "X-Signature": sign(PROVIDER_SECRET, raw),
    }
    url = f"{SERVICE_WEBHOOK_URL}/webhooks/providers/{provider_name}"
    # Retry на 404 — покрывает гонку «вебхук пришёл раньше, чем мы записали external_id».
    async with httpx.AsyncClient(timeout=10.0) as client:
        for attempt in range(5):
            try:
                r = await client.post(url, content=raw, headers=headers)
            except httpx.TransportError:
                await asyncio.sleep(0.2 * (attempt + 1))
                continue
            if r.status_code in (200, 201):
                return
            if r.status_code == 404:
                await asyncio.sleep(0.2 * (attempt + 1))
                continue
            return
