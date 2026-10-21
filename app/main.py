from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import Depends, FastAPI, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.models import Attempt, Task
from app.services import payments as payments_service
from app.services import tasks as tasks_service
from app.services import webhooks as webhooks_service
from app.services.watchdog import run_forever

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")


# ---------------------------------------------------------------------------
# Lifespan: сторож в каждом воркере (правило 7)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Сторож запускается в КАЖДОМ процессе uvicorn (4 штуки).
    # Задвоения нет благодаря FOR UPDATE SKIP LOCKED в watchdog.
    watchdog_task = asyncio.create_task(run_forever(interval_seconds=1.0))
    try:
        yield
    finally:
        watchdog_task.cancel()
        try:
            await watchdog_task
        except asyncio.CancelledError:
            pass


app = FastAPI(lifespan=lifespan)


# ---------------------------------------------------------------------------
# Схемы запросов/ответов
# ---------------------------------------------------------------------------
class GenerateIn(BaseModel):
    user_id: UUID
    model_slug: str
    prompt: str
    ref_images: int = Field(ge=0)
    # Только для тестов: поведение фейковых провайдеров.
    # В проде игнорируется (фейковые провайдеры в проде не участвуют).
    behavior: dict | None = None


class GenerateOut(BaseModel):
    task_id: UUID


class AttemptOut(BaseModel):
    leg_index: int
    provider: str
    status: str
    error_type: str | None
    external_id: str | None
    created_at: str
    finished_at: str | None

    @classmethod
    def from_orm(cls, a: Attempt) -> AttemptOut:
        return cls(
            leg_index=a.leg_index,
            provider=a.provider,
            status=a.status.value,
            error_type=a.error_type.value if a.error_type else None,
            external_id=a.external_id,
            created_at=a.created_at.isoformat(),
            finished_at=a.finished_at.isoformat() if a.finished_at else None,
        )


class TaskOut(BaseModel):
    id: UUID
    status: str
    current_leg_index: int | None
    result_url: str | None
    attempts: list[AttemptOut]


class BalanceOut(BaseModel):
    balance: int
    ledger: list[dict]


# ---------------------------------------------------------------------------
# POST /generate
# ---------------------------------------------------------------------------


@app.post("/generate", response_model=GenerateOut, status_code=status.HTTP_201_CREATED)
async def generate(body: GenerateIn, session: AsyncSession = Depends(get_session)):
    try:
        task = await tasks_service.create_task(
            session,
            user_id=body.user_id,
            model_slug=body.model_slug,
            prompt=body.prompt,
            ref_images=body.ref_images,
            behavior=body.behavior,
        )
    except tasks_service.NoSuitableLeg as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    except tasks_service.InsufficientFunds as e:
        return JSONResponse(status_code=402, content={"detail": str(e)})
    except tasks_service.TooManyActiveTasks as e:
        return JSONResponse(status_code=429, content={"detail": str(e)})
    except LookupError as e:
        return JSONResponse(status_code=404, content={"detail": str(e)})
    return GenerateOut(task_id=task.id)


# ---------------------------------------------------------------------------
# Вебхуки
# ---------------------------------------------------------------------------


@app.post("/webhooks/payments")
async def payment_webhook(request: Request, session: AsyncSession = Depends(get_session)):
    raw = await request.body()
    sig = request.headers.get("X-Signature")
    try:
        webhooks_service.verify_signature(settings.payment_webhook_secret, raw, sig)
    except webhooks_service.InvalidSignature as e:
        return JSONResponse(status_code=401, content={"detail": str(e)})

    import json

    try:
        body = json.loads(raw)
        payment_id = body["payment_id"]
        user_id = UUID(body["user_id"])
        tokens = int(body["tokens"])
        st = body["status"]
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "bad payload"})

    await payments_service.apply_payment(
        session, payment_id=payment_id, user_id=user_id, tokens=tokens, status=st
    )
    return {"ok": True}


@app.post("/webhooks/providers/{name}")
async def provider_webhook(
    name: str, request: Request, session: AsyncSession = Depends(get_session)
):
    raw = await request.body()
    sig = request.headers.get("X-Signature")
    try:
        secret = settings.provider_secret(name)
    except KeyError:
        return JSONResponse(status_code=404, content={"detail": f"unknown provider {name}"})
    try:
        webhooks_service.verify_signature(secret, raw, sig)
    except webhooks_service.InvalidSignature as e:
        return JSONResponse(status_code=401, content={"detail": str(e)})

    import json

    try:
        event = json.loads(raw)
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "bad json"})

    try:
        await webhooks_service.handle_provider_webhook(session, provider=name, event=event)
    except webhooks_service.UnknownAttempt as e:
        # Провайдер повторит (см. retry в fake_providers).
        return JSONResponse(status_code=404, content={"detail": str(e)})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    return {"ok": True}


# ---------------------------------------------------------------------------
# Чтение
# ---------------------------------------------------------------------------


@app.get("/tasks/{task_id}", response_model=TaskOut)
async def get_task(task_id: UUID, session: AsyncSession = Depends(get_session)):
    task = (await session.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
    if task is None:
        return JSONResponse(status_code=404, content={"detail": "task not found"})
    attempts = list(
        (
            await session.execute(
                select(Attempt).where(Attempt.task_id == task_id).order_by(Attempt.leg_index)
            )
        )
        .scalars()
        .all()
    )
    return TaskOut(
        id=task.id,
        status=task.status.value,
        current_leg_index=task.current_leg_index,
        result_url=task.result_url,
        attempts=[AttemptOut.from_orm(a) for a in attempts],
    )


@app.get("/users/{user_id}/balance", response_model=BalanceOut)
async def get_balance(user_id: UUID, session: AsyncSession = Depends(get_session)):
    try:
        balance, entries = await payments_service.get_balance(session, user_id)
    except LookupError as e:
        return JSONResponse(status_code=404, content={"detail": str(e)})
    return BalanceOut(
        balance=balance,
        ledger=[
            {
                "kind": e.kind.value,
                "delta": e.delta,
                "task_id": str(e.task_id) if e.task_id else None,
                "payment_id": e.payment_id,
                "created_at": e.created_at.isoformat(),
            }
            for e in entries
        ],
    )
