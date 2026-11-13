# FF Manager — менеджер проверок

Сервис оркестрирует проверки архитектуры (fitness functions): скрипты в каталоге `scripts/` и внешние HTTP-проверки по URL из БД. Скрипты можно добавлять и удалять без перезапуска — список перечитывается при запросах.

## Требования

- Python 3.10+
- PostgreSQL (схема `ff` и таблицы создаются при старте приложения)

## Установка

```bash
pip install -r requirements.txt
```

Опционально создайте `.env` в корне проекта (или задайте переменные с префиксом `FF_`):

```
FF_DB_HOST=localhost
FF_DB_PORT=5432
FF_DB_USER=myuser
FF_DB_PASSWORD=mysecretpassword
FF_DB_NAME=mydatabase
FF_SCRIPTS_DIR=scripts
FF_API_BASE_URL=http://127.0.0.1:8000
FF_EXTERNAL_FF_TIMEOUT_SECONDS=30
FF_FDM_PRODUCT_API_BASE_URL=http://localhost:8084
FF_DOCUMENTS_API_BASE_URL=
FF_DOCUMENTS_API_TIMEOUT_SECONDS=60
```

`FF_API_BASE_URL` — базовый URL сервиса для скриптов проверок (запись результата через API). `FF_EXTERNAL_FF_TIMEOUT_SECONDS` — таймаут HTTP POST при вызове внешней проверки (`fitness_function.method`). `FF_FDM_PRODUCT_API_BASE_URL` — URL products-service для получения `structurizrApiKey`/`Secret`. `FF_DOCUMENTS_API_BASE_URL` — базовый URL document-сервиса: загрузка документа по `docId` перед запуском FF и создание workspace-документа в `POST /api/v1/run-all-workspace` (`FF_DOCUMENTS_API_TIMEOUT_SECONDS` — таймаут этой загрузки).

## Запуск

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Интерактивная документация: [Swagger UI](http://127.0.0.1:8000/docs), схема OpenAPI: `/openapi.json`.

## API (префикс `/api/v1/`)

Служебные точки без префикса версии:

- **GET /health** — проверка доступности (для probes).
- **GET /docs**, **GET /openapi.json** — документация FastAPI.

Версионированное API:

| Метод | Путь | Назначение |
|--------|------|------------|
| GET | `/api/v1/` | Краткая информация о сервисе и ссылка на документацию |
| GET | `/api/v1/scripts` | Коды всех скриптов проверок (файлы `*.py` в каталоге, кроме `_*.py` и `__init__.py`) |
| GET | `/api/v1/fitness-functions` | Список записей `fitness_function` из БД |
| PUT | `/api/v1/fitness-function/{code}` | Создание или обновление проверки (`multipart`, upsert; статус **TEST**) |
| POST | `/api/v1/fitness-function/{code}/status` | Смена статуса: **TEST**, **TRIAL**, **ADOPT** |
| GET | `/api/v1/ff/call/{callId}` | Статус/результат асинхронного вызова (для тестовых — детали без `product_ff`); в ответе `ff_code` и `ff_name` |
| POST | `/api/v1/run/{code}` | Запуск одной проверки для приложения; тело JSON: `{"app": "<мнемоника>"}`; query `docId` (опц.) |
| POST | `/api/v1/run-all` | Запуск всех подходящих проверок; тело: `{"app": "<мнемоника>"}`; query `docId` (опц.) |
| POST | `/api/v1/run-all-workspace` | Расчёт всех проверок (**TRIAL**/**ADOPT**) по **workspace.json** в теле запроса; query `app` (обяз.) и `branch`; ничего не сохраняется, асинхронные внешние проверки не запускаются |
| POST | `/api/v1/ff/webhook` | Колбэк внешней проверки после вызова `method` |
| POST | `/api/v1/product/{code}/ff` | Запись результата проверки в `product_ff` (используется скриптами через `run_check` в `_common.py`) |
| GET | `/api/v1/product/{code}/actual-results` | Актуальные результаты по продукту: основные (по умолчанию) или вспомогательные (`?auxiliary=true`); без TEST |

Запуск одной проверки: либо исполняется скрипт `{code}.py`, либо выполняется POST на URL из поля `method` в `fitness_function`. Проверки со статусом **TEST** не входят в `run-all`.

`POST /api/v1/run-all-workspace` — «сухой» расчёт без сохранения: тело запроса — сам `workspace.json` (JSON-документ workspace), сервис создаёт документ в document-service (`POST /api/v2/documents/workspace/json?fileName=workspace.json&isPublic=true`), его `docId` уходит query-параметром синхронным внешним проверкам, а скриптам workspace передаётся в `data`. Запускаются только **TRIAL**/**ADOPT**; `applicability` проверяется по актуальным записям `product_ff` и по успешным результатам этого же вызова; асинхронные внешние проверки (`method_synchronous=false`) пропускаются. В `product_ff` и `outside_ff` ничего не записывается. Ограничение: скрипт без `execute()`, записывающий результат сам через `run_check()`, в этом режиме не запускается.

В ответе `run-all-workspace` (а также `run-all` и `run/{code}`) у каждой проверки есть `code` и `name`; `name` берётся из `fitness_function.description` и равен `null`, если записи в БД нет или описание пустое.

```bash
curl -X POST 'http://localhost:8000/api/v1/run-all-workspace?app=<alias>&branch=main' \
  -H 'Content-Type: application/json' --data-binary @workspace.json
```

### Ошибки клиента (4xx)

Для всех маршрутов под `/api/v1/`, **кроме** корня `GET /api/v1/`, ответы с кодами 400–499 отдаются в виде JSON с **единственным** полем:

```json
{ "errorMessage": "Текст на русском языке" }
```

Для `GET /health` и `GET /api/v1/` сохраняется стандартный формат FastAPI (`detail`). Полное описание возможных кодов по операциям — в Swagger.

## Скрипты проверок

Имя файла без `.py` — код проверки (например, `DEMOFF-1.py` → `DEMOFF-1`). Скрипт вызывается с одним аргументом — мнемоникой приложения.

Переменные окружения при запуске скрипта задаёт сервис: `FF_DB_*`, `FF_API_BASE_URL`, а для HMAC-клиента также `FF_STRUCTURIZR_HTTP_*` и `FF_STRUCTURIZR_API_*`. Запись результата выполняется POST-запросом на `{FF_API_BASE_URL}/api/v1/product/{alias}/ff` (см. `scripts/_common.py`).

## База данных

Схема **`ff`**, таблицы:

- **`ff.fitness_function`** — проверки: `code`, `description`, `applicability`, `auxiliary_check`, `status` (TEST / TRIAL / ADOPT), `script`, `method` (URL внешнего POST).
- **`ff.outside_ff`** — контекст асинхронных внешних вызовов (`call_id`, `product_code`, статус).
- **`ff.product_ff`** — результаты: `product_code`, `ff_id`, `is_check`, `is_actual`, `json_details`, `count_detail`, `success_detail`, `create_date`.

При первом старте в `fitness_function` добавляются тестовые строки **DEMOFF-1** и **DEMOFF-2**, если их ещё нет. Отдельная таблица каталога продуктов не используется: продукт задаётся строкой `product_code`.

