# План: имя фитнес-функции в ответе внешнего API

## Статус

- Ветка: `feature/run-all-workspace-ff-name` (создана от `develop`, HEAD `57a7586`).
- План применён: P1 + P2, код и тесты в рабочем дереве, коммитов и push нет.
- Внешняя документация (`../documentation`) не менялась — отдельный MR.

## 1. Задача

Внешний API `POST /api/v1/run-all-workspace` сейчас отдаёт по каждой проверке
только её **код**: код — ключ словаря `results` и единственное поле-идентификатор
в элементах `skipped`. Клиенту, чтобы показать человекочитаемое название, нужно
отдельно вызвать `GET /api/v1/fitness-functions` и сопоставить ответ по `code`.

Нужно расширить ответ: рядом с кодом отдавать **имя фитнес-функции**, не ломая
существующий контракт.

### Сейчас

```json
{
  "app": "DEMOFF",
  "branch": "main",
  "docId": 123,
  "results": {
    "DEMOFF-1": {
      "success": true,
      "message": "Проверка выполнена",
      "check_result": {"is_check": true, "details": [], "countDetail": 0, "successDetail": 0},
      "status": "ADOPT"
    }
  },
  "skipped": [
    {"code": "DEMOFF-3", "reason": "условия applicability не выполнены ..."}
  ]
}
```

### Целевой вид

```json
{
  "app": "DEMOFF",
  "branch": "main",
  "docId": 123,
  "results": {
    "DEMOFF-1": {
      "code": "DEMOFF-1",
      "name": "Проверка изоляции слоёв",
      "success": true,
      "message": "Проверка выполнена",
      "check_result": {"is_check": true, "details": [], "countDetail": 0, "successDetail": 0},
      "status": "ADOPT"
    }
  },
  "skipped": [
    {
      "code": "DEMOFF-3",
      "name": "Проверка обязательных ADR",
      "reason": "условия applicability не выполнены ..."
    }
  ]
}
```

Изменения только аддитивные: существующие поля, ключи, порядок и HTTP-коды
не меняются.

## 2. Что считать именем фитнес-функции

В `ff.fitness_function` **нет отдельной колонки `name`** (`db.py`, DDL —
`db.py:895-904`). Человекочитаемое название хранится в `description`; эта же
колонка используется в products-service (`product.local_fitness_function.description`)
и уже отдаётся как `ff_description` в `GET /api/v1/product/{code}/actual-results`
(`main.py:586-606`).

**Решение:** в ответе вводится поле `name`, значением которого становится
`description` из `ff.fitness_function`, обрезанное по краям.

Почему не добавляем новую колонку `name`:

- в домене (products-service, dashboard, ff-manager) уже есть `description`,
  и именно она используется как название;
- отдельная колонка потребует миграции, backfill, изменения upsert-формы
  `PUT /api/v1/fitness-function/{code}`, моделей и всех потребителей — это
  отдельная задача, а не расширение ответа;
- если позже появится отдельное короткое имя, контракт `name` не изменится —
  поменяется только источник значения.

Правила для значения:

| Ситуация | `name` |
|---|---|
| Строка ФФ есть, `description` непустой | `description.strip()` |
| Строка ФФ есть, `description` NULL/пустой/пробелы | `null` |
| Проверка только на диске (`scripts/*.py`), строки в БД нет | `null` |
| Дубликаты/регистр кода | берётся строка, найденная по точному коду, как и сейчас для `status` |

Подставлять `code` вместо пустого имени **не нужно**: клиент должен отличать
«имени нет» от «имя совпадает с кодом».

## 3. Контракт ответа

| Решение | Значение |
|---|---|
| Имя поля | `name` |
| Где | в каждом объекте `results[<code>]` и в каждом элементе `skipped[]` |
| Тип | `string \| null`; поле **присутствует всегда** |
| `code` внутри объекта результата | добавляется (дублирует ключ словаря) — чтобы элемент был самодостаточным при итерации по значениям |
| Форма `results` | остаётся объектом `{code: result}`, **не** массивом |
| Сортировка | не меняется |
| `message`/`check_result`/`status`/`reason` | не меняются |
| Версия API | не меняется, новый префикс/эндпоинт не нужен |
| Обратная совместимость | аддитивная: старые поля и типы не меняются; клиенты, игнорирующие новые поля, работают как раньше |

Почему `results` не превращаем в массив: это сломает существующих потребителей
(например, fdm-bpm для `run-all` сейчас читает ответ как `Object`). Ключ словаря
остаётся каноническим кодом.

Почему не `description`: потребитель просит «имя», а `description` в публичном
контракте уже занято семантикой описания в других эндпоинтах. `name` —
короткое однозначное имя поля; источник при этом документируется.

## 4. Объём работ

### P1 — обязательный (запрос пользователя)

`POST /api/v1/run-all-workspace`:

- `results[code].name`;
- `results[code].code`;
- `skipped[].name`.

### P2 — рекомендую включить в тот же MR (консистентность публичного API)

- `POST /api/v1/run-all` — те же `name`/`code` в `results` и `name` в `skipped`;
- `POST /api/v1/run/{code}` — `name` в корне ответа;
- `GET /api/v1/ff/call/{callId}` — `ff_name` рядом с существующим `ff_code`.

Это те же несколько строк и один общий helper; если оставить только
`run-all-workspace`, клиентам придётся по-разному получать имя в зависимости
от эндпоинта.

### Вне объёма

- `GET /api/v1/product/{code}/actual-results` — там уже есть `ff_description`;
  добавлять второй алиас `ff_name` в этой задаче не нужно.
- `GET /api/v1/fitness-functions` — уже отдаёт `description`.
- `GET /api/v1/dashboard/fitness-function` — уже отдаёт `description`.
- Изменение схемы БД, миграции, backfill.
- Изменение исходящих запросов внешних проверок и webhook-контракта.
- Замена `results` на массив / API v2.
- UI и сторонние репозитории, кроме документации (см. §7).

## 5. Реализация

### 5.1. Общий helper в `main.py`

Рядом с `_fitness_function_response_from_row` (`main.py:858`) или
`_runnable_ff_codes_ordered` (`main.py:957`):

```python
def _ff_name_from_row(row: Optional[dict]) -> Optional[str]:
    """Человекочитаемое имя ФФ для ответов API: description без пробелов по краям."""
    if not row:
        return None
    raw = row.get("description")
    if raw is None:
        return None
    name = str(raw).strip()
    return name or None
```

Helper не ходит в БД и не бросает исключений — его можно использовать во всех
эндпоинтах.

### 5.2. `POST /api/v1/run-all-workspace` — `_run_all_workspace_impl`

Файл: `main.py:1260-1396`.

1. `rows_by_code` уже строится из `get_all_fitness_functions()`
   (`main.py:1287-1291`) — **новых SQL-запросов не добавляем**, берём имя из
   этой же строки.
2. В `_run_code` (`main.py:1304-1322`) добавить в объект результата:

   ```python
   row = rows_by_code.get(code)
   results[code] = {
       "code": code,
       "name": _ff_name_from_row(row),
       "success": ok,
       "message": msg,
       "check_result": check_result,
       "status": fitness_function_status_from_row(row) if row else None,
   }
   ```

3. В трёх блоках формирования `skipped` (`main.py:1354-1382`) добавить `name`
   в каждый элемент:

   ```python
   {
       "code": c,
       "name": _ff_name_from_row(rows_by_code.get(c)),
       "reason": "...",
   }
   ```

   Это касается всех трёх групп: асинхронные, невыполненная applicability,
   статус TEST. Для TEST-кодов строки в БД есть, имя заполнится; для
   скрипта только на диске — `null`.

4. Верхний уровень `out` (`main.py:1384-1390`) не меняется.
5. В docstring эндпоинта (`main.py:1421-1434`) добавить абзац: `name` —
   название проверки из `fitness_function.description`, `null` — если записи
   в БД нет или описание пустое.

### 5.3. `POST /api/v1/run-all` — `run_all`

Файл: `main.py:1085-1238`.

1. Заменить `ff_status_by_code` (`main.py:1115-1127`) на карту строк
   «первая строка по коду»:

   ```python
   ff_by_code: dict[str, dict] = {}
   for r in get_all_fitness_functions():
       c = r.get("code")
       if c and c not in ff_by_code:
           ff_by_code[c] = r
   ```

   Тогда в обоих местах формирования результата (`main.py:1168-1175` и
   `main.py:1201-1208`):

   ```python
   row = ff_by_code.get(code)
   results[code] = {
       "code": code,
       "name": _ff_name_from_row(row),
       "success": ok,
       "message": msg,
       "check_result": check_result,
       "status": fitness_function_status_from_row(row) if row else FF_STATUS_TEST,
   }
   ```

2. В `skipped` (`main.py:1211-1227`) добавить `name` аналогично §5.2.
3. Поведение, включая `status = TEST` для кодов без строки в БД, сохраняется.

Необязательная оптимизация (можно не делать в этом MR): `run_all` сейчас
вызывает `get_all_fitness_functions()` дважды — внутри
`_runnable_ff_codes_ordered` (`main.py:957-981`) и для карты статусов.
Можно добавить параметр `rows: Optional[list[dict]] = None` и передавать
один результат. Это уменьшит число запросов, но не влияет на контракт.

### 5.4. `POST /api/v1/run/{code}` — `run_one`

Файл: `main.py:989-1079`.

`ff_row` уже загружен. В `out` (`main.py:1065-1073`) добавить:

```python
out: dict[str, Any] = {
    "code": run_code,
    "name": _ff_name_from_row(ff_row),
    "app": body.app,
    ...
}
```

`name` присутствует всегда; для скрипта только на диске — `null`.
Поле `status` по-прежнему добавляется только при наличии `ff_row` — это
существующее поведение, его не меняем.

### 5.5. `GET /api/v1/ff/call/{callId}` — `get_ff_call_result`

Файл: `main.py:1467-1502`, запрос в `db.py:450-471`.

1. В `db.py::get_outside_ff_call` добавить в `SELECT`:
   `ff.description AS ff_name`.
2. В `main.py` в `base` (`main.py:1483-1490`) добавить
   `"ff_name": row.get("ff_name")`.
3. Имя поля — `ff_name`, чтобы не смешивать с существующим `ff_code`
   в этом ответе.

### 5.6. БД

Изменений схемы нет. Все нужные колонки уже выбираются:

- `get_all_fitness_functions()` — `description` (`db.py:611-629`);
- `get_fitness_function_by_code()` — `description` (`db.py:285-303`);
- для §5.5 добавляется только чтение `description` в join.

## 6. Проверка

### 6.1. Юнит-тесты (без новой зависимости)

В репозитории не было каталога `tests/` и тестового фреймворка в
`requirements.txt`. Добавлен `tests/test_ff_name_in_responses.py` на
stdlib `unittest` + `unittest.mock`:

- `_ff_name_from_row`: `None`, пустая строка, пробелы, обычное значение;
- `_run_all_workspace_impl` с замоканными `get_all_fitness_functions`,
  `get_fitness_function_applicabilities`, `get_fitness_function_code_to_id`,
  `get_fitness_function_codes_excluded_from_run_all`,
  `fetch_structurizr_credentials`, `upload_workspace_json`,
  `run_ff_check_no_persist`: проверить `name` в `results` и `skipped`,
  `name = null` для кода только на диске, сохранность старых полей;
- `run_all` и `run_one` — аналогично для P2.

Запуск: `python -m unittest discover -s tests -v`.
Если тесты не добавляем, п. 6.2 обязателен.

### 6.2. Ручной контрактный тест

```bash
docker compose up --build -d

curl -s -X POST 'http://localhost:8000/api/v1/run-all-workspace?app=DEMOFF&branch=main' \
  -H 'Content-Type: application/json' \
  --data-binary @workspace.json | tee /tmp/ff-response.json

# У каждого результата есть code/name; name — строка или null
jq -e '.results | to_entries | all(.[];
  (.value | has("code") and has("name"))
  and (.value.code == .key)
  and ((.value.name | type) == "string" or .value.name == null)
)' /tmp/ff-response.json

# В skipped у каждого элемента есть name
jq -e '.skipped | all(.[]; has("code") and has("name") and has("reason"))' \
  /tmp/ff-response.json

# Старые поля не пропали
jq -e '.results | to_entries | all(.[]; (.value | has("success") and has("message") and has("check_result") and has("status")))' \
  /tmp/ff-response.json
```

Дополнительно:

1. Проверить проверку со статусом TEST: она попадает в `skipped`, `name`
   заполнен.
2. Проверить скрипт только на диске (без строки в `fitness_function`):
   `name = null`, `status = null`.
3. Проверить проверку с пустым `description` в БД: `name = null`.
4. Убедиться, что число запросов к БД не выросло: имя берётся из уже
   загруженных строк.
5. Для P2 — те же проверки для `run-all`, `run/{code}`, `ff/call/{callId}`.
6. Смоук CI (`docker compose up --wait`) не должен измениться: новые поля
   ни на что не влияют.

## 7. Документация

В этом репозитории:

- `main.py` — docstring `run_all_workspace`, `run_all`, `run_one`,
  `get_ff_call_result` (кратко: `name`/`ff_name`, источник, `null`).
- `README.md` — в описании `POST /api/v1/run-all-workspace` (строка ~61 и
  абзац ~68) упомянуть `name` в `results`/`skipped`.
- `docs/http-check-sync.md` / `docs/http-check-async.md` — только если там
  появится пример ответа; сейчас ответы `run-all-workspace` подробно не
  описаны, обязательных правок нет.
- У `run-all-workspace` сейчас нет `response_model`, поэтому новые поля не
  попадут в машинную схему OpenAPI автоматически. В этой задаче достаточно
  docstring и README; отдельной задачей можно добавить `response_model` или
  пример `200` в `responses`, не меняя сериализацию.

В соседнем репозитории `documentation` (вне этого рабочего дерева):

- `documentation/ff-manager/api/POST__api_v1_run-all-workspace.md` — модель
  ответа и пример;
- для P2 — `POST__api_v1_run-all.md`, `POST__api_v1_run_code.md`,
  `GET__api_v1_ff_call_call_id.md`;
- `documentation/ff-manager/Общее описание сервиса.md` — только если нужно
  упоминание состава ответа.

Эти правки не входят в ветку ff-manager; их нужно оформить отдельным MR в
репозитории документации.

## 8. Риски и совместимость

| Риск | Оценка | Мера |
|---|---|---|
| Старые клиенты сломаются | Низкий: поля аддитивные, форма `results` не меняется | Не менять ключи и типы; `name` всегда присутствует |
| `description` пустой/длинный | Средний: возможны `null` и длинные строки | Явно документировать `null`; не обрезать и не подменять кодом |
| Клиент ждёт `description`, а получит `name` | Низкий | В документации указать источник; при необходимости добавить `description`-алиас отдельной задачей |
| Лишние запросы к БД | Низкий | Использовать уже загруженные `rows_by_code`/`ff_by_code` |
| Расхождение имён между эндпоинтами | Средний | Единый helper `_ff_name_from_row`; в `ff/call` — `ff_name` по аналогии с `ff_code` |
| Ошибка в `name` ломает основной сценарий | Низкий | Helper не бросает исключений, при отсутствии данных возвращает `null` |

Откат: удалить новые поля и helper; схема БД и внешние вызовы не затрагиваются.

## 9. Чек-лист внедрения

- [x] Создать helper `_ff_name_from_row`.
- [x] `run-all-workspace`: `code` + `name` в `results`, `name` в `skipped`.
- [x] `run-all`: `code` + `name` в `results`, `name` в `skipped`.
- [x] `run/{code}`: `name` в корне.
- [x] `ff/call/{callId}`: `ff_name` (P2).
- [x] Обновить docstring/OpenAPI и README.
- [x] Добавить тесты на новые поля.
- [ ] Прогнать ручные проверки из §6.2 на стенде с БД.
- [ ] Проверить, что `product_ff`/`outside_ff` не затрагиваются (интеграционный прогон).
- [ ] Отдельный MR в `documentation` (вне этой ветки).

## 10. Принятые решения

1. **Источник имени.** `name` = `fitness_function.description` (trim), без новой
   колонки. Если потребуется отдельное короткое имя — отдельная задача с
   миграцией и изменением upsert.
2. **Объём.** Реализованы P1 (`run-all-workspace`) и P2 (`run-all`, `run/{code}`,
   `ff/call`).
3. **Имя поля.** `name` в результатах запуска; `ff_name` в `ff/call` — по
   аналогии с существующим `ff_code`.
4. **`actual-results`.** Не менялся: там уже есть `ff_description`.
