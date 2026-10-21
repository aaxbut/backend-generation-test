import asyncio
import hashlib
import hmac
import json
import os

import pytest

from tests.conftest import wait_until

PAY_SECRET = os.environ.get("PAYMENT_WEBHOOK_SECRET", "pay-secret")
BETA_SECRET = os.environ.get("PROVIDER_BETA_SECRET", "beta-secret")


def _sign(secret: str, raw: bytes) -> str:
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


async def _topup(client, db, user_id, tokens=100):
    body = {
        "payment_id": "pay-" + os.urandom(8).hex(),
        "user_id": str(user_id),
        "tokens": tokens,
        "status": "paid",
    }
    raw = json.dumps(body, separators=(",", ":")).encode()
    r = await client.post(
        "/webhooks/payments",
        content=raw,
        headers={"X-Signature": _sign(PAY_SECRET, raw), "content-type": "application/json"},
    )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_10_concurrent_failed_on_last_leg_single_refund(client, db, fresh_user):
    await _topup(client, db, fresh_user, tokens=100)

    # ref_images=5 → alpha не подходит, первая (и последняя) нога — beta.
    r = await client.post(
        "/generate",
        json={
            "user_id": str(fresh_user),
            "model_slug": "image-fast",
            "prompt": "cat",
            "ref_images": 5,
            "behavior": {"silent": True},
        },
    )
    assert r.status_code == 201, r.text
    task_id = r.json()["task_id"]

    async def _ext():
        return await db.fetchval(
            "SELECT external_id FROM attempts WHERE task_id = $1 AND leg_index = 1", task_id
        )

    await wait_until(lambda: _ext(), timeout=15)  # без is not None
    external_id = await _ext()
    assert external_id is not None

    body = {"external_id": external_id, "status": "failed", "error_type": "technical"}
    raw = json.dumps(body, separators=(",", ":")).encode()
    headers = {"X-Signature": _sign(BETA_SECRET, raw), "content-type": "application/json"}

    results = await asyncio.gather(
        *[client.post("/webhooks/providers/beta", content=raw, headers=headers) for _ in range(10)]
    )
    assert all(r.status_code == 200 for r in results), [r.status_code for r in results]

    assert await db.fetchval("SELECT status FROM tasks WHERE id = $1", task_id) == "failed"
    assert (
        await db.fetchval(
            "SELECT count(*) FROM ledger WHERE kind = 'refund' AND task_id = $1", task_id
        )
        == 1
    )
    assert await db.fetchval("SELECT balance FROM users WHERE id = $1", fresh_user) == 100

    total = await db.fetchval(
        "SELECT COALESCE(SUM(delta), 0) FROM ledger WHERE user_id = $1", fresh_user
    )
    assert total == 100
