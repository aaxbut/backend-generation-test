from __future__ import annotations

import logging
import uuid

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LedgerEntry, LedgerKind, Payment, User

log = logging.getLogger(__name__)


class PaymentIgnored(Exception):
    """Платёж уже обработан (дубль вебхука) или status != paid."""


async def apply_payment(
    session: AsyncSession,
    *,
    payment_id: str,
    user_id: uuid.UUID,
    tokens: int,
    status: str,
) -> None:
    """
    Начисление ровно один раз.

    Вся работа — в одной транзакции:
      1. INSERT INTO payments ... ON CONFLICT (payment_id) DO NOTHING RETURNING payment_id
         Вставилось → мы «владелец» этого платежа.
         Не вставилось → дубль, выходим (200, без изменений).
      2. UPDATE users SET balance = balance + tokens RETURNING balance
      3. INSERT INTO ledger (kind='topup', delta=+tokens, payment_id=...)

    Падение между 1–3 откатывает всё: и payments, и balance, и ledger.
    Повторный вебхук снова пройдёт шаг 1 и начислит.
    """
    if status != "paid":
        # Неоплаченный/неуспешный статус не двигает деньги и не «занимает»
        # payment_id — если провайдер позже пришлёт paid, он будет обработан.
        log.info("payment %s ignored: status=%s", payment_id, status)
        return

    async with session.begin():
        ins = (
            pg_insert(Payment)
            .values(
                payment_id=payment_id,
                user_id=user_id,
                tokens=tokens,
                status=status,
            )
            .on_conflict_do_nothing(index_elements=[Payment.payment_id])
            .returning(Payment.payment_id)
        )
        inserted = (await session.execute(ins)).scalar_one_or_none()
        if inserted is None:
            # Дубль: строку вставил кто-то другой (в т.ч. параллельно).
            # Никаких изменений — 200 наверх.
            log.info("payment %s duplicate ignored", payment_id)
            return

        await session.execute(
            update(User).where(User.id == user_id).values(balance=User.balance + tokens)
        )

        session.add(
            LedgerEntry(
                user_id=user_id,
                kind=LedgerKind.topup,
                delta=tokens,
                payment_id=payment_id,
            )
        )


async def get_balance(session: AsyncSession, user_id: uuid.UUID) -> tuple[int, list[LedgerEntry]]:
    balance = (
        await session.execute(select(User.balance).where(User.id == user_id))
    ).scalar_one_or_none()
    if balance is None:
        raise LookupError(f"user {user_id} not found")

    entries = (
        (
            await session.execute(
                select(LedgerEntry)
                .where(LedgerEntry.user_id == user_id)
                .order_by(LedgerEntry.created_at.desc(), LedgerEntry.id.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    return balance, list(entries)
