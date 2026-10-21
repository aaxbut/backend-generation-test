import hashlib
import hmac
import json
import os

import pytest

from tests.conftest import wait_until

PAY_SECRET = os.environ.get("PAYMENT_WEBHOOK_SECRET", "pay-secret")
ALPHA_SECRET = os.environ.get("PROVIDER_ALPHA_SECRET", "alpha-secret")
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
async def test_first_leg_fail_then_second_leg_completes(client, db, fresh_user):
    """
    image-fast: alpha(max_refs=4) → beta(max_refs=10).
    ref_images=2 → первая нога alpha.
    Шлём failed(technical) от alpha → задача уходит на beta → шлём completed от beta.
    В истории две попытки: (alpha, failed, technical), (beta, completed).
    """
    await _topup(client, db, fresh_user, tokens=100)

    r = await client.post(
        "/generate",
        json={
            "user_id": str(fresh_user),
            "model_slug": "image-fast",
            "prompt": "cat",
            "ref_images": 2,
            "behavior": {"fail_rate": 1.0, "fail_error_type": "technical"},
        },
    )
    assert r.status_code == 201, r.text
    task_id = r.json()["task_id"]

    # Ждём первую попытку (alpha, leg_index=0) с external_id.
    async def _ext0():
        return await db.fetchval(
            "SELECT external_id FROM attempts WHERE task_id = $1 AND leg_index = 0", task_id
        )

    await wait_until(lambda: _ext0(), timeout=15)
    ext0 = await _ext0()

    # failed(technical) от alpha.
    body = {"external_id": ext0, "status": "failed", "error_type": "technical"}
    raw = json.dumps(body, separators=(",", ":")).encode()
    r = await client.post(
        "/webhooks/providers/alpha",
        content=raw,
        headers={"X-Signature": _sign(ALPHA_SECRET, raw), "content-type": "application/json"},
    )
    assert r.status_code == 200

    # Вторая нога создана и провайдер выдал external_id.
    async def _ext1():
        return await db.fetchval(
            "SELECT external_id FROM attempts WHERE task_id = $1 AND leg_index = 1", task_id
        )

    await wait_until(lambda: _ext1(), timeout=15)
    ext1 = await _ext1()

    # completed от beta.
    body = {"external_id": ext1, "status": "completed", "result_url": "https://fake/x"}
    raw = json.dumps(body, separators=(",", ":")).encode()
    r = await client.post(
        "/webhooks/providers/beta",
        content=raw,
        headers={"X-Signature": _sign(BETA_SECRET, raw), "content-type": "application/json"},
    )
    assert r.status_code == 200

    status = await db.fetchval("SELECT status FROM tasks WHERE id = $1", task_id)
    assert status == "completed"

    rows = await db.fetch(
        "SELECT leg_index, provider, status, error_type FROM attempts "
        "WHERE task_id = $1 ORDER BY leg_index",
        task_id,
    )
    assert len(rows) == 2
    assert rows[0]["leg_index"] == 0 and rows[0]["provider"] == "alpha"
    assert rows[0]["status"] == "failed" and rows[0]["error_type"] == "technical"
    assert rows[1]["leg_index"] == 1 and rows[1]["provider"] == "beta"
    assert rows[1]["status"] == "completed"

    # Деньги не возвращались.
    n_refunds = await db.fetchval(
        "SELECT count(*) FROM ledger WHERE kind = 'refund' AND task_id = $1", task_id
    )
    assert n_refunds == 0
