# Generation Service

Сервис генерации по ТЗ: FastAPI + PostgreSQL + SQLAlchemy 2.0 (async) + Alembic.
Работает в 4 воркера uvicorn, поднимает двух фейковых провайдеров, обеспечивает
«ровно один раз» для начисления и возврата, закрывает зависшие попытки сторожем.

## Содержание

- [Быстрый старт](#быстрый-старт)
- [Ручная проверка](#ручная-проверка)
- [Запуск тестов](#запуск-тестов)
- [Архитектура и гарантии](#архитектура-и-гарантии)
  - [«Ровно один раз»: начисление](#ровно-один-раз-начисление)
  - [«Ровно один раз»: возврат](#ровно-один-раз-возврат)
  - [Баланс и журнал](#баланс-и-журнал)
  - [Лимит активных задач](#лимит-активных-задач)
  - [Жизненный цикл попытки](#жизненный-цикл-попытки)
  - [Отправка на следующую ногу](#отправка-на-следующую-ногу)
- [Падение процесса посреди вебхука](#падение-процесса-посреди-вебхука)
- [Правило 6: поздний `completed`](#правило-6-поздний-completed)
- [Сторож и 4 воркера](#сторож-и-4-воркера)
- [Миграции: как добавить следующую](#миграции-как-добавить-следующую)
- [Отличия от исходного дизайна](#отличия-от-исходного-дизайна)
- [Структура репозитория](#структура-репозитория)
- [Известные ограничения](#известные-ограничения)

---

## Быстрый старт

Требуется: Docker + Docker Compose v2, `make`, `curl`, `jq`.

```bash
make up      # сборка образа, db + migrate + api×4 + provider-alpha + provider-beta
make ps      # статус контейнеров
make logs    # логи всех сервисов
make down    # погасить (данные БД сохраняются)
make nuke    # полный сброс: контейнеры + тома + образы
```

Сервис: `http://localhost:8000`, OpenAPI: `http://localhost:8000/docs`.
Провайдеры: `localhost:9001` (alpha), `localhost:9002` (beta).

Сервис `migrate` — one-shot: прогоняет `alembic upgrade head` **один раз** до
старта API. 4 воркера не гоняются за миграциями.

## Ручная проверка

`psql -tA` печатает статус `INSERT 0 1` вместе с результатом, а иногда и `\r` —
поэтому UUID вытаскиваем регуляркой, а не «как есть». Это самый частый источник
ошибки `invalid character ... found 'I'` при ручном прогоне.

```bash
# 1. Пользователь с балансом 100
USER_ID=$(docker compose exec -T db psql -U app -d app -tA \
  -c "INSERT INTO users (id, balance) VALUES (gen_random_uuid(), 100) RETURNING id" \
  | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | head -n1)
echo "USER_ID=[$USER_ID]"

# 2. Генерация
RESP=$(curl -s -X POST http://localhost:8000/generate \
  -H 'content-type: application/json' \
  -d "{\"user_id\":\"$USER_ID\",\"model_slug\":\"image-fast\",\"prompt\":\"cat\",\"ref_images\":2}")
echo "$RESP"
TASK_ID=$(echo "$RESP" | jq -r '.task_id // empty')

# 3. Подождать вебхук провайдера (2–5 c)
sleep 6

# 4. Состояние задачи и история попыток
curl -s http://localhost:8000/tasks/$TASK_ID | jq .

# 5. Баланс и журнал
curl -s http://localhost:8000/users/$USER_ID/balance | jq .
```

Ожидаемо:

- `POST /generate` → `201 {"task_id":"..."}`;
- `GET /tasks/{id}` → `status: "completed"`, `attempts: [{leg_index:0, provider:"alpha", status:"completed"}]`;
- `GET /users/{id}/balance` → `balance: 98`, в `ledger` две строки: `topup +100`, `charge -2`.

**Альтернатива без прямого `INSERT`** — начислить баланс через настоящий
платёжный вебхук с HMAC (как в тестах). Так проверяется и подпись:

```bash
USER_ID=$(docker compose exec -T db psql -U app -d app -tA \
  -c "INSERT INTO users (id) VALUES (gen_random_uuid()) RETURNING id" \
  | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | head -n1)

PAYLOAD=$(printf '{"payment_id":"pay-%s","user_id":"%s","tokens":100,"status":"paid"}' \
  "$(openssl rand -hex 6)" "$USER_ID")
SIG=$(printf '%s' "$PAYLOAD" | openssl dgst -sha256 -hmac "pay-secret" -hex | awk '{print $2}')

curl -s -X POST http://localhost:8000/webhooks/payments \
  -H "content-type: application/json" -H "X-Signature: $SIG" -d "$PAYLOAD"
```

## Запуск тестов

```bash
make test
```

Что делает `make test`:

1. `docker compose down -v` — чистый старт, чтобы 20 оплат и лимит 3 активных
   задач не пересекались между тестами.
2. Поднимает compose с `PROVIDER_TIMEOUT=2`, `PROVIDER_SYNC_TIMEOUT=2`
   (тест 4 ждёт сторожа, а не 60 с).
3. Ждёт `GET /docs`.
4. Запускает `pytest -x -q tests` в сервисе `tests` (профиль `tests`, в обычном
   `up` не поднимается).
5. Гасит окружение.

Четыре обязательных сценария (все против реальной БД из compose):

| Тест | Что проверяет | Ключевая защита |
|---|---|---|
| `test_payment_idempotent` | 20 одновременных одинаковых вебхуков оплаты → одно начисление | `ON CONFLICT DO NOTHING RETURNING` + `uq_ledger_topup_per_payment` |
| `test_failed_last_leg` | 10 одновременных `failed` на последней ноге → один возврат | CAS `attempt.pending→failed` + CAS `task.running→failed` + `uq_ledger_refund_per_task` |
| `test_first_leg_fail` | отказ первой ноги → задача на второй, в истории две попытки | `_advance_or_refund` + `send_attempt` после коммита |
| `test_late_completed` | поздний `completed` после сторожа → правило 6 | `timed_out` окончателен + `uq_ledger_late_ignored_per_task` |

### Поведение провайдеров per-request

Чтобы тесты были детерминированы, поведение фейковых провайдеров задаётся
полем `behavior` в `POST /generate`. Оно пробрасывается в `POST /tasks` к
провайдеру **первой ноги**; для последующих ног `behavior` не передаётся
(там работает поведение по умолчанию — `completed`).

```json
{
  "user_id": "...",
  "model_slug": "image-fast",
  "prompt": "cat",
  "ref_images": 2,
  "behavior": {"fail_rate": 1.0, "fail_error_type": "technical"}
}
```

| Поле | Смысл |
|---|---|
| `fail_rate` | вероятность `failed` в авто-вебхуке |
| `fail_error_type` | `technical` / `moderation` / `payload` |
| `sync_status` | код ответа на `POST /tasks` (≥500 — синхронный отказ) |
| `sync_timeout` | «зависнуть» дольше `PROVIDER_SYNC_TIMEOUT` |
| `duplicate_webhook` | прислать вебхук дважды подряд |
| `concurrent_duplicate` | два вебхука одновременно |
| `silent` | не присылать вебхук (сторож закроет по таймауту) |
| `late_completed` | `completed` позже `PROVIDER_TIMEOUT` |

В проде фейковых провайдеров нет — поле игнорируется. Это тестовый хук,
зафиксированный в `GenerateIn.behavior`.

### Если тесты гоняются с хоста

```bash
PROVIDER_TIMEOUT=2 docker compose up -d --force-recreate api
pytest -q tests   # SERVICE_URL=http://localhost:8000 по умолчанию
```

---

## Архитектура и гарантии

Все денежные и статусные переходы живут в одной транзакции БД. HTTP к
провайдеру **никогда** не выполняется внутри транзакции.

Важное правило SQLAlchemy 2.0: **любой `SELECT` до `async with session.begin():`
запускает autobegin**, и явный `begin()` после него падает с
`A transaction is already begun`. Поэтому весь `SELECT`, участвующий в решении,
идёт **внутри** `begin()`, а поиск попытки по `external_id` (`_resolve_attempt`)
выполняется уже в транзакции `complete_attempt`/`fail_attempt_and_advance`.

### «Ровно один раз»: начисление

Гарантия держится на **первичном ключе `payments.payment_id`** и
`INSERT ... ON CONFLICT DO NOTHING RETURNING`:

```python
ins = (
    pg_insert(Payment)
    .values(payment_id=..., user_id=..., tokens=..., status="paid")
    .on_conflict_do_nothing(index_elements=[Payment.payment_id])
    .returning(Payment.payment_id)
)
inserted = (await session.execute(ins)).scalar_one_or_none()
if inserted is None:
    return  # дубль, 200, ничего не меняем
await session.execute(update(User).values(balance=User.balance + tokens))
session.add(LedgerEntry(kind="topup", delta=+tokens, payment_id=...))
```

- 20 параллельных вебхуков: ровно один получит строку из `RETURNING` и начислит;
  остальные получат `None` и выйдут с 200.
- `status != "paid"` обрабатывается **до** вставки в `payments`: неоплаченный
  вебхук не «занимает» `payment_id`, и поздний `paid` с тем же id будет обработан.
- Вторая линия — частичный уникальный индекс `uq_ledger_topup_per_payment`
  на `ledger(payment_id) WHERE kind='topup'`.

### «Ровно один раз»: возврат

Возврат делает только тот, кто выиграл **compare-and-set** по попытке:

```sql
UPDATE attempts SET status='failed', error_type=$1, finished_at=now()
WHERE id=$2 AND status='pending'
RETURNING id
```

Проигравший CAS не делает ничего. Выигравший — в **той же транзакции** —
решает «следующая нога или возврат». Возврат — второй CAS:

```sql
UPDATE tasks SET status='failed', finished_at=now(), current_leg_index=NULL
WHERE id=$1 AND status='running'
RETURNING id
```

Только выигравший пишет `ledger(kind='refund', delta=+price)` и
`users.balance += price`. Третья линия — уникальный индекс
`uq_ledger_refund_per_task`.

### Баланс и журнал

Инвариант: `users.balance == SUM(ledger.delta) WHERE user_id = users.id`.
Каждое изменение баланса сопровождается строкой журнала **в той же
транзакции** (правило 8 ТЗ).

Списание при создании задачи — под `SELECT ... FOR UPDATE` по пользователю:

```sql
UPDATE users SET balance = balance - $price
WHERE id = $uid AND balance >= $price
RETURNING balance
```

### Лимит активных задач

Проверка лимита и списание идут под `SELECT ... FOR UPDATE` по строке
пользователя в одной транзакции:

```
BEGIN
  SELECT * FROM users WHERE id=$uid FOR UPDATE
  SELECT count(*) FROM tasks WHERE user_id=$uid AND status='running'
  -- >= 3 → 429
  UPDATE users SET balance = balance - price WHERE id=$uid AND balance >= price
  INSERT INTO tasks (...)
  INSERT INTO ledger (kind='charge', delta=-price, task_id=...)
  INSERT INTO attempts (status='pending', deadline_at=now()+timeout)
COMMIT
```

Без блокировки два параллельных `POST /generate` оба увидели бы `active=2` и
создали бы третью → 4 активных. Частичный индекс `ix_tasks_user_active`
ускоряет `count(*)`.

### Жизненный цикл попытки

```
                POST /tasks 202
   ┌─ pending ────────────────────────┐
   │     │                             │
   │     │ вебхук completed            │ сторож: deadline_at <= now()
   │     ▼                             ▼
   │  completed                    timed_out  ← окончательный статус
   │                                   │
   │ вебхук failed (technical/         │ поздний completed
   │ moderation/payload)               ▼
   ▼                              late_ignored (delta=0,
  failed ──► следующая нога          задача не меняется)
             или возврат
```

Правила выбора следующей ноги (`_next_suitable_leg`, правило 2 ТЗ):

- `technical` → следующая нога;
- `moderation` → следующая, **кроме** `skip_on_moderation`;
- `payload` → `None` (задача падает сразу, деньги возвращаются);
- «следующая» — строго `leg_index > current_leg_index` в порядке каталога.

Правило 1: нога подходит, если `max_refs >= ref_images`. Не подходит ни одна —
400 **до** списания.

### Отправка на следующую ногу

`_advance_or_refund` создаёт `Attempt(pending)` на следующей ноге, но **не
делает HTTP**. HTTP выполняется **после коммита** транзакции, создавшей
попытку:

- `create_task` → `providers_client.send_attempt(...)` для первой ноги;
- `fail_attempt_and_advance` → после `begin()` перечитывает новую попытку и
  вызывает `send_attempt(...)`;
- `watchdog.process_one_batch` → собирает `new_ids` внутри транзакции с
  `SKIP LOCKED`, после коммита шлёт `send_attempt(...)` для каждой.

Почему так: если отправить внутри транзакции, HTTP будет держать блокировки
секундами (таймаут провайдера), а если не отправить вообще — попытка «мёртвая»
с момента создания, и сторож закроет её по `PROVIDER_TIMEOUT` → ложный возврат.
Это был реальный баг в процессе разработки: тест `test_first_leg_fail`
показывал `current=0 → beta`, но `POST /tasks` к beta не уходил, и через 2 с
сторож делал refund.

---

## Падение процесса посреди вебхука

Вся обработка вебхука — **одна транзакция**: проверка HMAC (в памяти, до
транзакции) → `_resolve_attempt` → CAS попытки → решение «следующая нога /
возврат» → запись в `tasks`, `users`, `ledger`, `attempts`.

Если процесс падает на любом шаге — Postgres откатывает транзакцию целиком.
Состояние в БД остаётся таким, каким было до вебхука: попытка `pending`, задача
`running`, деньги не двигались.

Дальше:

1. **Провайдер повторяет вебхук** (фейковые провайдеры ретраят на 404 и сетевые
   ошибки). Второй вебхук проходит CAS и завершает переход. Гарантия «ровно
   один раз» не страдает: CAS идемпотентен.
2. **Провайдер не повторяет.** Попытка осталась `pending` с `deadline_at` в
   прошлом — сторож (в любом из 4 воркеров) заберёт её через
   `SELECT ... FOR UPDATE SKIP LOCKED` и переведёт в `timed_out`, дальше —
   следующая нога или возврат.

Частично применённого состояния не бывает. Именно поэтому HTTP к провайдеру
вынесен из транзакции.

## Правило 6: поздний `completed`

Ситуация: сторож закрыл попытку как `timed_out`, задачу как `failed`, деньги
вернул. Через секунду приходит `completed`.

**Решение: `timed_out` окончателен.** Поздний `completed`:

1. Пытается CAS `pending → completed` — проигрывает (статус `timed_out`).
2. Проверяет `attempt.status == timed_out` → пишет
   `ledger(kind='late_ignored', delta=0, task_id=...)` через
   `ON CONFLICT DO NOTHING` по индексу `uq_ledger_late_ignored_per_task`.
3. Задачу **не трогает**: `tasks.status` остаётся `failed`, `result_url` не
   заполняется, деньги не двигаются.

Почему так, а не «принять поздний результат и откатить возврат»:

- **«Один раз» становится двухфазным.** Принять поздний результат = отменить
  `refund` и вернуть `charge`. Вторая денежная операция, которая конкурирует с
  другими вебхуками и сторожем. Риск двойного возврата резко растёт.
- **Один переход определяет и статус, и деньги.** «`timed_out` → `failed` +
  возврат» атомарен. «`timed_out` → `completed` + отмена возврата» — два
  перехода, которые могут разъехаться при падении между ними.
- **Минус честно назван:** провайдер мог потратить ресурсы впустую. Это видно
  в `ledger` (`late_ignored`) и в логах. Деньги пользователя защищены сильнее,
  чем ресурсы провайдера.

Идемпотентность `late_ignored` — частичный уникальный индекс
`uq_ledger_late_ignored_per_task` + `ON CONFLICT DO NOTHING`: 10 дублей
позднего `completed` дадут ровно одну строку.

Тест: `tests/test_late_completed.py` — `PROVIDER_TIMEOUT=2`, `behavior={"silent": true}`,
сторож закрывает попытку, ручной поздний `completed` → задача `failed`, ровно
один `refund`, ровно одна `late_ignored`, баланс не изменился; повторный
поздний `completed` не добавляет вторую `late_ignored`.

---

## Сторож и 4 воркера

`uvicorn --workers 4` запускает 4 процесса. В каждом — свой `lifespan`, который
поднимает `asyncio.Task` с `run_forever(interval_seconds=1.0)`. Итого 4 сторожа.

Задвоения нет благодаря `SELECT ... FOR UPDATE SKIP LOCKED`:

```python
attempts_rows = (
    await session.execute(
        select(Attempt)
        .where(Attempt.status == AttemptStatus.pending, Attempt.deadline_at <= now)
        .with_for_update(skip_locked=True)
        .limit(limit)
    )
).scalars().all()
for a in attempts_rows:
    new_id = await _timed_out_in_tx(session, attempt=a)
```

- `FOR UPDATE` лочит строки до коммита.
- `SKIP LOCKED` заставляет второй воркер **пропустить** залоченные строки, а не
  ждать. 4 сторожа не блокируют друг друга и не берут одну попытку дважды.
- Claim и обработка — **в одной транзакции на батч**. Если разнести `SELECT` и
  `UPDATE`, блокировка снимется между ними.
- `_timed_out_in_tx` всё равно делает CAS `WHERE status='pending'` — вторая
  страховка на случай гонки с вебхуком.
- HTTP (`send_attempt` для новых попыток) — **после** коммита.

---

## Миграции: как добавить следующую

```bash
# Новая пустая ревизия
make revision m="add index on tasks.created_at"

# Применить до head
make migrate

# Откатить на одну
docker compose run --rm --no-deps -T migrate alembic downgrade -1

# Полный сброс и накат
docker compose down -v
make up
```

Правила:

1. **Ревьюим автогенерацию.** Частичные индексы (`postgresql_where`) и
   `server_default` не всегда генерятся корректно.
2. **Enum-типы дропаем явно в `downgrade`.** Postgres не удаляет `CREATE TYPE`
   при `drop_table`. Без `DROP TYPE IF EXISTS ...` повторный `upgrade` упадёт
   на «type already exists».
3. **Данные — отдельной data-миграцией.** `0002_catalog.py` — пример:
   `op.bulk_insert` для `models` и `legs`. Новая модель — новая ревизия, без
   правки `0002`.
4. **Частичные уникальные индексы — отдельными ревизиями, если добавляются
   после `0001`.** Пример — `0003_late_ignored_index.py`:

```python
revision = "0003"
down_revision = "0002"

def upgrade() -> None:
    op.create_index(
        "uq_ledger_late_ignored_per_task",
        "ledger",
        ["task_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'late_ignored'"),
    )

def downgrade() -> None:
    op.drop_index("uq_ledger_late_ignored_per_task", table_name="ledger")
```

Пример новой модели:

```python
def upgrade() -> None:
    op.bulk_insert(
        sa.table("models", sa.column("slug", sa.String), sa.column("price", sa.Integer)),
        [{"slug": "video-short", "price": 10}],
    )
    op.bulk_insert(
        sa.table(
            "legs",
            sa.column("model_slug", sa.String),
            sa.column("leg_index", sa.Integer),
            sa.column("provider", sa.String),
            sa.column("max_refs", sa.Integer),
            sa.column("skip_on_moderation", sa.Boolean),
        ),
        [{"model_slug": "video-short", "leg_index": 0, "provider": "alpha",
          "max_refs": 3, "skip_on_moderation": False}],
    )
```

---

## Отличия от исходного дизайна

Дизайн из первого файла принят целиком; ниже — уточнения, найденные при
написании кода и тестов.

| Место | В дизайне | В коде | Почему |
|---|---|---|---|
| Оплата, `status != paid` | «вставилась строка → начисляем» | неоплаченный статус **не** пишет `payments` | иначе поздний `paid` с тем же `payment_id` был бы проигнорирован как дубль — деньги потеряны |
| Поздний `completed` | «пишем `late_ignored`» | `late_ignored` через `ON CONFLICT DO NOTHING` + частичный уникальный индекс | без этого 10 дублей позднего `completed` дали бы 10 `late_ignored` |
| `attempts.py` | публичные функции с `session.begin()` | приватные `_*_in_tx` + публичные обёртки | сторожу нужно удержать `SKIP LOCKED` от `SELECT` до `UPDATE` в одной транзакции |
| Выбор ноги | не описан явно | `_next_suitable_leg` по правилам 1–2 | ТЗ требует 400 до списания и `skip_on_moderation` |
| 4xx от провайдера | — | трактуем как `technical` | в ТЗ нет маппинга кодов; безопаснее увести на следующую ногу |
| `Task.price` | не фиксировался | фиксируется на момент создания | возврат = `+task.price` совпадает с `charge`; каталог мог поменяться |
| Отправка последующих ног | не сказано, кто шлёт `POST /tasks` | `send_attempt` **после коммита** в `fail_attempt_and_advance` и `watchdog` | иначе HTTP висит под блокировкой, либо попытка «мёртвая» и сторож делает ложный refund |
| `behavior` в `/generate` | — | тестовый хук, пробрасывается в `POST /tasks` | все 4 теста в одном прогоне, без перезапуска compose с разными env |
| `migrate` | — | отдельный one-shot сервис | 4 воркера не должны гоняться за `alembic upgrade head` |

---

## Структура репозитория

```
app/
  main.py              # FastAPI, роуты, lifespan со сторожем
  config.py            # settings, database_url, sqlalchemy_url, таймауты, секреты
  db.py                # create_async_engine(settings.sqlalchemy_url), sessionmaker
  models.py            # модели + частичные уникальные индексы
  services/
    payments.py        # начисление ровно раз, чтение баланса
    tasks.py           # create_task: выбор ноги, FOR UPDATE, списание, лимит 3
    providers_client.py# send_attempt: HTTP вне транзакции, sync_error → failed(technical)
    attempts.py        # _resolve_attempt, CAS-переходы, _*_in_tx, late_ignored
    webhooks.py        # HMAC на сыром теле, диспетчеризация
    watchdog.py        # FOR UPDATE SKIP LOCKED, send_attempt после коммита
alembic/
  env.py               # async-версия, config.set_main_option(..., settings.sqlalchemy_url)
  versions/
    0001_init.py       # схема + частичные индексы + enum-типы
    0002_catalog.py    # данные каталога (models, legs)
    0003_late_ignored_index.py  # uq_ledger_late_ignored_per_task
fake_providers/
  common.py            # behavior, HMAC, retry на 404
  alpha.py
  beta.py
tests/
  conftest.py          # client, db, fresh_user, wait_until (awaitable-aware)
  test_payment_idempotent.py
  test_failed_last_leg.py
  test_first_leg_fail.py
  test_late_completed.py
docker-compose.yml
Dockerfile
Makefile
requirements.txt
README.md
```

---

## Известные ограничения

- **Нет авторизации и регистрации.** `user_id` принимается как есть (ТЗ).
- **Нет настоящего хранилища файлов.** `result_url` — строка-ссылка от
  провайдера.
- **Нет настоящих денег.** `payments` — дедупликация вебхуков; `tokens` —
  условные единицы.
- **Фейковые провайдеры в проде не участвуют.** `behavior` в `/generate` —
  тестовый хук.
- **`late_ignored` не откатывает возврат.** Осознанно (см. «Правило 6»);
  провайдер мог потратить ресурсы впустую — видно в логах и журнале.
- **`PROVIDER_TIMEOUT=60` по умолчанию.** Для тестов `make test` ставит 2.
- **`behavior` не пробрасывается на последующие ноги.** Если понадобится —
  сохраним в `Task`; пока все сценарии тестов укладываются в первую ногу.

---

## Частые ошибки при ручной проверке

| Симптом | Причина | Фикс |
|---|---|---|
| `invalid character ... found 'I'` в `user_id` | `psql -tA` допечатал `INSERT 0 1` в переменную | вытащить UUID через `grep -oE '...'` |
| `JSON decode error: Invalid control character` | в теле `curl` оказался `\r` от `psql` | `tr -d '\r\n'` или `grep -oE` |
| `402 Insufficient funds` | пользователь создан без баланса | `INSERT ... balance=100` или платёжный вебхук |
| `422 uuid_parsing` | `USER_ID` пустой или с мусором | проверить `echo "[$USER_ID]"` |
| `TASK_ID=[]` | `/generate` вернул ошибку, `jq` не нашёл `task_id` | смотреть сырой ответ `curl -i` |