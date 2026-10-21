# Makefile
# Единая точка входа: поднять / погасить / прогнать тесты / посмотреть логи.
# Все цели идемпотентны, кроме test-up/test-down — они управляют жизненным циклом.

SHELL := /bin/bash
COMPOSE := docker compose
PYTEST := $(COMPOSE) run --rm --no-deps -T tests pytest
SERVICE_URL ?= http://localhost:8000

# Значения по умолчанию для тестового прогона (переопределяют compose).
TEST_PROVIDER_TIMEOUT ?= 2
TEST_SYNC_TIMEOUT ?= 2

.PHONY: help up down restart logs ps build migrate revision test test-ci lint clean nuke

help: ## Показать список целей
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

up: build ## Поднять всё (db, migrate, api×4, provider-alpha, provider-beta)
	$(COMPOSE) up -d
	@echo "Waiting for API..."
	@for i in $$(seq 1 60); do \
		if curl -sf $(SERVICE_URL)/docs >/dev/null 2>&1; then \
			echo "API is up: $(SERVICE_URL)"; exit 0; \
		fi; \
		sleep 0.5; \
	done; \
	echo "API did not start in time" >&2; $(COMPOSE) logs api | tail -50; exit 1

down: ## Погасить контейнеры (данные БД сохраняются)
	$(COMPOSE) down

restart: down up ## Перезапустить

build: ## Пересобрать образ
	$(COMPOSE) build

ps: ## Статус контейнеров
	$(COMPOSE) ps

logs: ## Логи всех сервисов (Ctrl-C для выхода)
	$(COMPOSE) logs -f --tail=200

migrate: ## Прогнать миграции до head (на запущенной БД)
	$(COMPOSE) run --rm --no-deps -T migrate alembic upgrade head

revision: ## Новая миграция: make revision m="add index"
	@if [ -z "$(m)" ]; then echo "usage: make revision m=\"message\"" >&2; exit 1; fi
	$(COMPOSE) run --rm --no-deps -T migrate alembic revision -m "$(m)"

# ---------------------------------------------------------------------------
# Тесты
#
# Тесты идут против реального API из compose (ТЗ: «могут ходить в настоящую
# базу из compose»). Поведение провайдеров задаётся per-request через поле
# behavior в POST /generate, поэтому один прогон покрывает все 4 теста.
# Единственное, что нужно переопределить — PROVIDER_TIMEOUT для теста 4:
# сторож должен сработать за ~2 c, а не за 60.
# ---------------------------------------------------------------------------

test: ## Прогнать 4 обязательных теста (поднимает изолированный compose)
	@echo ">>> Сбрасываю окружение и поднимаю с PROVIDER_TIMEOUT=$(TEST_PROVIDER_TIMEOUT)"
	$(COMPOSE) down -v --remove-orphans
	PROVIDER_TIMEOUT=$(TEST_PROVIDER_TIMEOUT) PROVIDER_SYNC_TIMEOUT=$(TEST_SYNC_TIMEOUT) \
		$(COMPOSE) up -d --build
	@echo ">>> Жду готовности API"
	@for i in $$(seq 1 120); do \
		if curl -sf $(SERVICE_URL)/docs >/dev/null 2>&1; then break; fi; \
		sleep 0.5; \
		if [ $$i -eq 120 ]; then echo "API did not start" >&2; $(COMPOSE) logs api | tail -80; exit 1; fi; \
	done
	@echo ">>> Запускаю тесты"
	$(COMPOSE) run --rm --no-deps -T \
		-e SERVICE_URL=http://api:8000 \
		-e DATABASE_URL=postgresql://app:app@db:5432/app \
		tests pytest -x -q tests
	@echo ">>> Тесты прошли"
	$(COMPOSE) down -v --remove-orphans

test-ci: ## То же, что test, но без down в конце (для CI с артефактами логов)
	@echo ">>> Сбрасываю окружение и поднимаю с PROVIDER_TIMEOUT=$(TEST_PROVIDER_TIMEOUT)"
	$(COMPOSE) down -v --remove-orphans
	PROVIDER_TIMEOUT=$(TEST_PROVIDER_TIMEOUT) PROVIDER_SYNC_TIMEOUT=$(TEST_SYNC_TIMEOUT) \
		$(COMPOSE) up -d --build
	@for i in $$(seq 1 120); do \
		if curl -sf $(SERVICE_URL)/docs >/dev/null 2>&1; then break; fi; \
		sleep 0.5; \
		if [ $$i -eq 120 ]; then echo "API did not start" >&2; $(COMPOSE) logs api | tail -80; exit 1; fi; \
	done
	$(COMPOSE) run --rm --no-deps -T \
		-e SERVICE_URL=http://api:8000 \
		-e DATABASE_URL=postgresql://app:app@db:5432/app \
		tests pytest -q tests
	@echo ">>> Не гашу окружение (test-ci)"

# ---------------------------------------------------------------------------
# Чистка
# ---------------------------------------------------------------------------

clean: ## Удалить контейнеры и образы проекта
	$(COMPOSE) down --rmi local --remove-orphans

nuke: ## Полный сброс: контейнеры, тома, образы
	$(COMPOSE) down -v --rmi local --remove-orphans
	docker volume prune -f

# ---------------------------------------------------------------------------
# Линт и форматирование
# ---------------------------------------------------------------------------

lint: ## Проверить код (ruff check, без правок)
	ruff check app tests fake_providers

format: ## Отформатировать код и отсортировать импорты
	ruff format app tests fake_providers
	ruff check --fix app tests fake_providers

check: lint ## lint + тесты (полная проверка перед коммитом)
	$(MAKE) test