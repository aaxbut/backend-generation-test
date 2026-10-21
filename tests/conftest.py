from __future__ import annotations

import asyncio
import inspect
import os
import uuid
from collections.abc import AsyncIterator

import asyncpg
import httpx
import pytest_asyncio

# ВАЖНО: тесты ходят в настоящую БД из compose (правило из ТЗ).
# Базовый URL сервиса берём из env, чтобы можно было запускать и локально.
SERVICE_URL = os.environ.get("SERVICE_URL", "http://localhost:8000")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://app:app@localhost:5432/app")


@pytest_asyncio.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=SERVICE_URL, timeout=30.0) as c:
        yield c


@pytest_asyncio.fixture
async def db() -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def fresh_user(db: asyncpg.Connection) -> uuid.UUID:
    """
    Новый пользователь на каждый тест — чтобы 20 вебхуков оплаты и лимит
    3 активных задач не пересекались между тестами.
    """
    uid = uuid.uuid4()
    await db.execute("INSERT INTO users (id, balance) VALUES ($1, 0)", uid)
    return uid


async def wait_until(cond, *, timeout: float = 10.0, interval: float = 0.1):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        res = cond()
        if inspect.isawaitable(res):
            res = await res
        if res:
            return
        await asyncio.sleep(interval)
    raise TimeoutError("condition not met in time")
