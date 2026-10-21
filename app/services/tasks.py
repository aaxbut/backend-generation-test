from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    Attempt,
    AttemptStatus,
    LedgerEntry,
    LedgerKind,
    Leg,
    Model,
    Task,
    TaskStatus,
    User,
)
from app.services import providers_client


class NoSuitableLeg(Exception):
    """Ни одна нога не принимает столько референсов → 400 до списания."""


class InsufficientFunds(Exception):
    """Баланс меньше цены → 402."""


class TooManyActiveTasks(Exception):
    """У пользователя уже MAX_ACTIVE_TASKS активных задач → 429."""


async def create_task(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    model_slug: str,
    prompt: str,
    ref_images: int,
    behavior: dict | None = None,
) -> Task:
    """
    Создание задачи + списание + первая отправка провайдеру.

    Транзакция 1 (короткая): проверка ног, блокировка пользователя,
    лимит активных, списание, создание Task, создание первой Attempt(pending).
    HTTP-вызов провайдера — ВНЕ транзакции (см. providers_client.send_task).
    Транзакция 2: сохранить external_id или перевести попытку в failed(technical).
    """
    # --- Проверка каталога (можно вне транзакции: данные статичны) ---
    async with session.begin():
        model = (
            await session.execute(select(Model).where(Model.slug == model_slug))
        ).scalar_one_or_none()
        if model is None:
            raise NoSuitableLeg(f"model {model_slug} not found")

        legs = (
            (
                await session.execute(
                    select(Leg).where(Leg.model_slug == model_slug).order_by(Leg.leg_index)
                )
            )
            .scalars()
            .all()
        )

        suitable = [leg for leg in legs if leg.max_refs >= ref_images]
        if not suitable:
            # Правило 1: не подходит ни одна — 400 ДО списания.
            raise NoSuitableLeg(f"no leg for model={model_slug} refs={ref_images}")

        first_leg = suitable[0]

        # --- Транзакция 1: списание и создание задачи ---
        # Блокируем строку пользователя: проверка лимита и списание
        # должны видеть одно и то же состояние под конкуренцией.
        user = (
            await session.execute(select(User).where(User.id == user_id).with_for_update())
        ).scalar_one_or_none()
        if user is None:
            raise LookupError(f"user {user_id} not found")

        active = (
            await session.execute(
                select(func.count())
                .select_from(Task)
                .where(Task.user_id == user_id, Task.status == TaskStatus.running)
            )
        ).scalar_one()
        if active >= settings.max_active_tasks:
            raise TooManyActiveTasks(f"user {user_id} has {active} active tasks")

        if user.balance < model.price:
            raise InsufficientFunds(f"balance={user.balance} price={model.price}")

        # Атомарное списание с проверкой в WHERE: даже если баланс
        # изменился между SELECT и UPDATE, мы не уйдём в минус.
        new_balance = (
            await session.execute(select(User.balance).where(User.id == user_id).with_for_update())
        ).scalar_one()
        if new_balance < model.price:
            raise InsufficientFunds("balance changed under lock")

        await session.execute(
            User.__table__.update()
            .where(User.id == user_id)
            .values(balance=User.balance - model.price)
        )

        task = Task(
            user_id=user_id,
            model_slug=model_slug,
            prompt=prompt,
            ref_images=ref_images,
            price=model.price,
            status=TaskStatus.running,
            current_leg_index=first_leg.leg_index,
        )
        session.add(task)
        await session.flush()  # нужен task.id для attempt и ledger

        session.add(
            LedgerEntry(
                user_id=user_id,
                kind=LedgerKind.charge,
                delta=-model.price,
                task_id=task.id,
            )
        )

        attempt = Attempt(
            task_id=task.id,
            leg_index=first_leg.leg_index,
            provider=first_leg.provider,
            status=AttemptStatus.pending,
            deadline_at=datetime.now(UTC) + timedelta(seconds=settings.provider_timeout),
        )
        session.add(attempt)
        await session.flush()

        task_id = task.id
        attempt_id = attempt.id

    # --- HTTP вне транзакции ---
    # Возвращаем задачу из БД, чтобы отдать актуальное состояние.
    # Отправка и её последствия обрабатываются в отдельной транзакции.
    await providers_client.dispatch_first_attempt(
        task_id=task_id,
        attempt_id=attempt_id,
        provider=first_leg.provider,
    )

    # Перечитываем задачу — attempt мог быть переведён в failed(technical).
    task = (await session.execute(select(Task).where(Task.id == task_id))).scalar_one()
    return task
