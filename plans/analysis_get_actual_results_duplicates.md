# Анализ: причины задвоения записей в get_actual_results_by_product_code

## 1. Постановка проблемы

Функция [`get_actual_results_by_product_code`](db.py:681) выполняет SELECT из `product_ff` с фильтром `pf.is_actual = true`. Сама по себе она **не порождает дубли** — она лишь возвращает то, что уже есть в БД.

**Корень проблемы — в том, что в таблице `product_ff` может оказаться несколько строк с `is_actual = true` для одной пары `(product_code, ff_id)`.**

Ниже — детальный разбор причин.

---

## 2. Race Condition в save_product_ff_result (основная причина)

Функция [`save_product_ff_result`](db.py:133) вызывается из `POST /api/v1/product/{code}/ff` и сохраняет результат проверки в `product_ff`. Она выполняет две операции последовательно:

```python
# Шаг 1: снять флаг is_actual со старых записей (строки 168-175)
UPDATE ff.product_ff
SET is_actual = false
WHERE LOWER(product_code) = LOWER(%s) AND ff_id = %s AND is_actual = true

# Шаг 2: вставить новую актуальную запись (строки 176-205)
INSERT INTO ff.product_ff (..., is_actual, ...)
VALUES (..., true, ...)
```

**Проблема:** эти два шага выполняются **в рамках одной транзакции, но без взятия блокировки на строки** (нет `SELECT ... FOR UPDATE`).

### Сценарий гонки (race condition)

```
Транзакция A                          Транзакция B
─────────────────────────────────────────────────────
UPDATE SET is_actual=false
  → сняла флаг со старой записи
                                      UPDATE SET is_actual=false
                                        → тоже сняла флаг (строк уже нет)
INSERT ... is_actual=true
  → создала запись #1 ✅
                                      INSERT ... is_actual=true
                                        → создала запись #2 ✅
COMMIT                                 COMMIT
```

**Результат:** две строки с `is_actual = true` для одной пары `(product_code, ff_id)`.

### Почему так происходит?

- PostgreSQL MVCC: `UPDATE` в каждой транзакции видит одно и то же состояние на момент начала операции.
- Отсутствует `SELECT ... FOR UPDATE`, который бы заставил транзакцию B ждать завершения транзакции A.
- Отсутствует partial unique index, который бы запретил две строки с `is_actual=true` для одной пары.

---

## 3. Та же проблема в process_ff_webhook

Функция [`process_ff_webhook`](db.py:309) вызывается из:
- `POST /api/v1/ff/webhook` (колбэк внешней асинхронной проверки)
- `_invoke_external_post` в [`ff_runner.py`](ff_runner.py:214) (синхронный HTTP-метод)

Она содержит **точно такой же паттерн** (строки 357-391):

```python
# Шаг 1 (строки 357-363)
UPDATE ff.product_ff
SET is_actual = false
WHERE product_code = %s AND ff_id = %s AND is_actual = true

# Шаг 2 (строки 365-391)
INSERT INTO ff.product_ff (..., is_actual, ...)
VALUES (..., true, ...)
```

**Примечание:** в [`process_ff_webhook`](db.py:321) есть `FOR UPDATE` на `outside_ff`, но нет `FOR UPDATE` на `product_ff`. Это защищает от дублей в `outside_ff`, но не в `product_ff`.

---

## 4. Отсутствие ограничения на уровне БД

Таблица `product_ff` (создаётся в [`init_schema`](db.py:871-882)) не имеет:

- **Partial unique index** вида:
  ```sql
  CREATE UNIQUE INDEX uq_product_ff_one_actual
  ON ff.product_ff (product_code, ff_id) WHERE is_actual = true;
  ```
- **UNIQUE constraint** на `(product_code, ff_id)` (он бы не подошёл, т.к. нужно разрешить несколько «неактуальных» записей)

Если бы такой индекс существовал, второй `INSERT` упал бы с `IntegrityError`, и дубли бы не появились.

---

## 5. Схематичная диаграмма потоков

```mermaid
flowchart TD
    A[POST /api/v1/product/{code}/ff] --> B[save_product_ff_result]
    B --> C[UPDATE is_actual=false]
    B --> D[INSERT is_actual=true]
    
    E[POST /api/v1/ff/webhook] --> F[process_ff_webhook]
    F --> G[UPDATE is_actual=false]
    F --> H[INSERT is_actual=true]
    
    I[Синхронная HTTP проверка] --> J[_invoke_external_post]
    J --> K[process_ff_webhook]
    K --> L[UPDATE is_actual=false]
    K --> M[INSERT is_actual=true]
    
    C --> N{Конкурентный доступ?}
    D --> N
    G --> N
    H --> N
    L --> N
    M --> N
    
    N -->|"Да (race condition)"| O[Две строки с is_actual=true]
    N -->|"Нет"| P[Одна строка с is_actual=true]
    
    O --> Q[get_actual_results_by_product_code<br/>возвращает >1 строки<br/>для одной пары]
    P --> R[get_actual_results_by_product_code<br/>возвращает 1 строку<br/>для одной пары]
```

---

## 6. Условия возникновения

Чтобы возник дубль, нужно **одновременное** (конкурентное) выполнение **любых двух** из следующих операций для одной пары `(product_code, ff_id)`:

1. Два параллельных `POST /api/v1/product/{code}/ff`
2. Два параллельных `POST /api/v1/ff/webhook` (с одним `ff_id`, но разными `call_id`)
3. Один `POST /api/v1/product/{code}/ff` и один `POST /api/v1/ff/webhook` одновременно
4. Синхронный HTTP-метод, вызывающий `process_ff_webhook`, параллельно с любым из вышеперечисленного

На практике это наиболее вероятно при:
- Повторных запусках `run-all` для одного продукта, когда несколько проверок финишируют одновременно
- Сценариях, где один и тот же скрипт вызывается несколько раз параллельно (например, через CI/CD pipeline с параллельными джобами)
- Синхронных внешних проверках с медленным ответом, когда клиент повторяет `POST /api/v1/product/{code}/ff`

---

## 7. Дополнительный фактор: множественные source_type / source_id

Хотя [`save_product_ff_result`](db.py:133) принимает `source_type` и `source_id`, эти поля **не участвуют** в поиске существующих записей для деактивации. Условие `UPDATE` использует только `product_code` и `ff_id`. Таким образом, разные источники могут конкурировать за одну и ту же пару, усугубляя проблему.

---

## 8. Рекомендации по исправлению

### Вариант A (рекомендуемый): Optimistic Locking через partial unique index

Добавить на уровне БД:

```sql
CREATE UNIQUE INDEX uq_product_ff_one_actual
ON ff.product_ff (product_code, ff_id) WHERE is_actual = true;
```

Плюсы:
- Защита от дублей на уровне БД — «железобетонно»
- Не нужно менять логику приложения
- При возникновении конфликта — `IntegrityError`, который можно поймать и повторить операцию

Минусы:
- Нужна миграция БД
- При существующих дублях индекс не создастся — нужно предварительно почистить данные

### Вариант B: Pessimistic Locking (SELECT FOR UPDATE)

Добавить в `save_product_ff_result` и `process_ff_webhook` блокировку перед UPDATE:

```python
cur.execute(
    f"""
    SELECT id FROM {SCHEMA}.product_ff
    WHERE LOWER(product_code) = LOWER(%s) AND ff_id = %s AND is_actual = true
    FOR UPDATE
    """,
    (code_key, ff_id),
)
```

Плюсы:
- Предотвращает гонку без изменения схемы БД

Минусы:
- Снижает конкурентность (последовательное выполнение)
- Не защищает от ошибок в будущем (если появится третий путь записи)
- Сложнее с `process_ff_webhook` — там уже есть `FOR UPDATE` на `outside_ff`, нужно согласовывать порядок блокировок

### Вариант C: UPSERT / ON CONFLICT

Переписать логику на `INSERT ... ON CONFLICT` — но это сложнее, т.к. нужно менять не одну строку, а деактивировать старую и вставлять новую.

### Рекомендация

**Оптимальный подход — комбинация A + B:**
1. Добавить partial unique index (защита на уровне БД)
2. Перед UPDATE выполнять `SELECT FOR UPDATE` (защита от гонки на уровне транзакций)
3. В коде, вызывающем `save_product_ff_result`/`process_ff_webhook`, добавить retry-логику на случай `IntegrityError` от уникального индекса

---

## 9. Дополнительно: подчистка существующих дублей

Перед добавлением unique index необходимо найти и почистить уже существующие дубли: для каждой пары `(product_code, ff_id)` оставить только одну (самую свежую) запись с `is_actual = true`, остальные перевести в `is_actual = false`.
