from __future__ import annotations

import hashlib
import hmac
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ErrorType
from app.services import attempts as attempts_service

log = logging.getLogger(__name__)


class InvalidSignature(Exception):
    """HMAC не сошёлся → 400/401."""


class UnknownAttempt(Exception):
    """external_id не найден → 404 (обычно значит, что провайдер шлёт
    вебхук раньше, чем мы записали external_id; провайдер повторит)."""


def verify_signature(secret: str, raw_body: bytes, signature: str | None) -> None:
    """
    Проверка ДО любой записи в БД и на СЫРОМ теле (не на перепакованном JSON).
    compare_digest — защита от тайминг-атак.
    """
    if not signature:
        raise InvalidSignature("missing X-Signature")
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise InvalidSignature("signature mismatch")


async def handle_provider_webhook(
    session: AsyncSession,
    *,
    provider: str,
    event: dict,
) -> None:
    external_id = event.get("external_id")
    status = event.get("status")
    if not external_id or status not in ("completed", "failed"):
        raise ValueError("bad webhook payload")

    if status == "completed":
        url = event.get("result_url")
        if not url:
            raise ValueError("completed without result_url")
        await attempts_service.complete_attempt(
            session, provider=provider, external_id=external_id, result_url=url
        )
        return

    raw_err = event.get("error_type")
    try:
        error_type = ErrorType(raw_err) if raw_err else ErrorType.technical
    except ValueError:
        error_type = ErrorType.technical

    await attempts_service.fail_attempt_and_advance(
        session, provider=provider, external_id=external_id, error_type=error_type
    )
