from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings

# asyncpg-драйвер. pool_pre_ping лечит «сервер закрыл соединение» между
# запросами в долгоживущем пуле 4 воркеров.
engine = create_async_engine(
    settings.sqlalchemy_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=5,
)

# expire_on_commit=False: после commit() объекты остаются читаемыми без
# дополнительного SELECT. Важно в вебхуках, где после commit нужен ответ.
SessionFactory = async_sessionmaker(
    engine,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """
    FastAPI dependency. Транзакцию НЕ открывает: сервисы открывают её сами
    через `async with session.begin():`, чтобы границы транзакции были видны
    в месте, где принимается решение «ровно один раз».
    """
    async with SessionFactory() as session:
        yield session
