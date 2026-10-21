from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- База ---
    database_url: str = Field(alias="DATABASE_URL")

    # --- Таймауты ---
    # Сколько ждём вебхук от провайдера, прежде чем сторож закроет попытку.
    # ТЗ: для тестов 60 секунд. Для теста 4 переопределяем через env.
    provider_timeout: int = Field(default=60, alias="PROVIDER_TIMEOUT")
    # Таймаут на синхронный POST /tasks к провайдеру.
    # Синхронный таймаут == failed technical (правило 3), поэтому он должен
    # быть заметно меньше provider_timeout, иначе «молчащий» провайдер
    # сначала повесит синхронный запрос.
    provider_sync_timeout: float = Field(default=5.0, alias="PROVIDER_SYNC_TIMEOUT")

    # --- Подписи ---
    payment_webhook_secret: str = Field(alias="PAYMENT_WEBHOOK_SECRET")
    provider_alpha_secret: str = Field(alias="PROVIDER_ALPHA_SECRET")
    provider_beta_secret: str = Field(alias="PROVIDER_BETA_SECRET")

    # --- Провайдеры ---
    provider_alpha_url: str = Field(alias="PROVIDER_ALPHA_URL")
    provider_beta_url: str = Field(alias="PROVIDER_BETA_URL")

    # --- Прочее ---
    # Лимит активных задач пользователя (правило про 429).
    max_active_tasks: int = Field(default=3, alias="MAX_ACTIVE_TASKS")

    def provider_url(self, name: str) -> str:
        return {
            "alpha": self.provider_alpha_url,
            "beta": self.provider_beta_url,
        }[name]

    def provider_secret(self, name: str) -> str:
        return {
            "alpha": self.provider_alpha_secret,
            "beta": self.provider_beta_secret,
        }[name]

    @property
    def sqlalchemy_url(self) -> str:
        url = self.database_url
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url


settings = Settings()
