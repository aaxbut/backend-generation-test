import asyncio
import hashlib
import hmac
import json
import os

import pytest

PAY_SECRET = os.environ.get("PAYMENT_WEBHOOK_SECRET", "pay-secret")


def _sign(raw: bytes) -> str:
    return hmac.new(PAY_SECRET.encode(), raw, hashlib.sha256).hexdigest()


@pytest.mark.asyncio
async def test_20_concurrent_payment_webhooks_single_credit(client, db, fresh_user):
    payment_id = "pay-" + os.urandom(8).hex()
    body = {
        "payment_id": payment_id,
        "user_id": str(fresh_user),
        "tokens": 100,
        "status": "paid",
    }
    raw = json.dumps(body, separators=(",", ":")).encode()
    headers = {"X-Signature": _sign(raw), "content-type": "application/json"}

    # 20 ОДНОВРЕМЕННЫХ запросов с идентичным телом и подписью.
    results = await asyncio.gather(
        *[client.post("/webhooks/payments", content=raw, headers=headers) for _ in range(20)]
    )
    # Все должны ответить 200 — дубли не ошибка.
    assert all(r.status_code == 200 for r in results), [r.status_code for r in results]

    # Ровно одно начисление: баланс 100, ровно одна строка topup.
    balance = await db.fetchval("SELECT balance FROM users WHERE id = $1", fresh_user)
    assert balance == 100

    n_payments = await db.fetchval(
        "SELECT count(*) FROM payments WHERE payment_id = $1", payment_id
    )
    assert n_payments == 1

    n_topups = await db.fetchval(
        "SELECT count(*) FROM ledger WHERE kind = 'topup' AND payment_id = $1", payment_id
    )
    assert n_topups == 1

    # Инвариант: баланс == сумма журнала.
    total = await db.fetchval(
        "SELECT COALESCE(SUM(delta), 0) FROM ledger WHERE user_id = $1", fresh_user
    )
    assert total == balance
