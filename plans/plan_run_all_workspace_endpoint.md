# POST /api/v1/run-all-workspace — прогон проверок по workspace.json из тела, без сохранения

## Context

Сейчас единственный сценарий массового запуска — `POST /api/v1/run-all?docId=...` (`main.py:1041`): вызывающий (fdm-bpm) обязан сам выгрузить JSON workspace из Structurizr (`StructurizrClient.getDocs` → `{structurizrApiUrl}/json`) и создать документ в document-service (`DocumentClient.postDocument` → `POST /api/v1/documents/workspace/json?isPublic=true`), чтобы передать `docId`. Результаты прогона при этом штатно пишутся в `product_ff` (для TRIAL/ADOPT), а асинхронные внешние проверки — в `outside_ff` и завершаются webhook'ом.

Нужен «сухой» режим: вызывающий отдаёт **чистый workspace.json прямо в теле** запроса (alias продукта — в query), ff-manager **сам** создаёт документ, прогоняет **все** проверки TRIAL/ADOPT, **ничего не сохраняет** и возвращает только JSON-ответ. Асинхронные внешние проверки не запускаются (их результат пришёл бы позже и некуда его писать).

Итог: BPM может отказаться от собственной выгрузки документа и получить полный результат расчёта одним синхронным вызовом.

## Согласованные требования

| # | Требование |
|---|------------|
| 1 | `POST /api/v1/run-all-workspace`, alias продукта — query `app`, ветка — существующий query `branch` |
| 2 | Тело запроса — сам workspace.json (JSON-объект, ~2.8 МБ в реальных образцах) |
| 3 | Запускаются только **TRIAL/ADOPT** (TEST исключаются); набор кодов — как у `run-all` |
| 4 | **applicability учитывается**, гибридно: предусловие выполнено, если есть актуальный успех в `product_ff` **или** соответствующая проверка дала `is_check=true` в этом же вызове |
| 5 | Ничего не пишется: ни `product_ff`, ни `outside_ff` |
| 6 | Асинхронные внешние проверки (`method` + `method_synchronous=false`) — в `skipped`; синхронные (`true`) — вызываются, результат из тела ответа, `docId` уходит query-параметром |
| 7 | Документ создаётся сервисом: `POST {FF_DOCUMENTS_API_BASE_URL}/api/v2/documents/workspace/json?fileName=workspace.json&isPublic=true`, тело — сырые байты JSON, только `urllib` |
| 8 | Ошибка создания документа → **502**, проверки не запускаются |
| 9 | Скриптам в `data` — распарсенный workspace; `docId` в env скриптов **не** передаём |
| 10 | Ответ: `{app, branch, docId, results: {CODE: {success, message, check_result, status}}, skipped: [{code, reason}]}` |
| 11 | Legacy-скрипты, сами вызывающие `run_check()` в `scripts/_common.py`, не глушим (гарантия «без сохранения» — на уровне раннера) |

## Реализация

### 1. `document_api.py` (новый модуль)

По образцу `product_api.py` (клиент внешнего сервиса + своё исключение):

```python
class DocumentApiError(Exception): ...

def upload_workspace_json(
    raw_json: bytes, *, file_name: str = "workspace.json", is_public: bool = True
) -> int:
    """POST /api/v2/... → docId. Бросает DocumentApiError."""
```

- Базовый URL — `settings.documents_api_base_url` (уже есть); новый параметр в `config.py`: `documents_api_timeout_seconds: float = 60.0` (тело в несколько МБ).
- `urllib.request.Request(url, data=raw_json, method="POST", headers={"Content-Type": "application/json"})` — `Content-Length` urllib выставляет сам (требование v2). Образец сценария: `structurizr-backend-new/tests/upload_workspace2doc.py`.
- Ошибки: пустой base url / не задан → `DocumentApiError`; `HTTPError` → код + `errorMessage` из тела (document-service отдаёт `{"errorMessage": "..."}`); `URLError` → сеть; не-JSON/нет `docId` → ошибка формата. Маппинг в HTTPException делает эндпоинт (как `main.py::_fetch_document_data`, строки 488–516).
- `requests` в `requirements.txt` есть, но `requests_toolbelt` — нет; multipart не нужен, используем `urllib` (как существующий `_fetch_document_data`).

### 2. `ff_runner.py`

**a. `_invoke_external_post`: минимальный рефакторинг — параметр `persist: bool = True`.**
При `persist=False`: не вызывать `insert_outside_ff_call`, `delete_outside_ff_by_call_id`, `process_ff_webhook`; при `method_synchronous=false` — сразу вернуть неуспех с внятным сообщением (эндпоинт такие проверки до вызова не доводит); при синхронном ответе собрать `check_result` из уже распарсенных `is_check/json_details/count_detail/success_detail` (переиспользовать чистую `run_result.check_result_from_stored_row`). Дефолт `True` сохраняет поведение `run`/`run-all` без изменений — риск минимальный, регрессия видна по существующим вызовам (`run_ff_check`).

**b. Новая функция `run_ff_check_no_persist` — аналог `run_ff_check`:**

```python
def run_ff_check_no_persist(
    code: str,
    app_mnemonic: str,
    *,
    structurizr_credentials: Optional[Tuple[str, str]] = None,
    data: Optional[dict[str, Any]] = None,
    doc_id: Optional[str] = None,
    branch: str = DEFAULT_BRANCH,
) -> Tuple[bool, str, CheckResultPayload]:
```

- `row = get_fitness_function_by_code(code)` → при непустом `method` и `method_synchronous` → `_invoke_external_post(..., persist=False, is_test_mode=True, doc_id=doc_id)`; при непустом `method` и `method_synchronous=false` → `(False, "Асинхронная внешняя проверка в этом режиме не запускается", None)`.
- Иначе `run_script(canonical_code, app_mnemonic, structurizr_credentials=..., data=data, is_test_mode=True, branch=branch)` — существующий режим «не сохранять» (`_run_script_module` при `is_test_mode=True` не вызывает `persist_execute_result` и возвращает `check_result` из `build_check_result_from_details_list`). `source_type`/`source_id` не передаём: сохранять нечего.
- Известный косметический дефект: для скрипта **без** `execute()` fallback-subprocess в этом режиме возвращает текст «Тестовая проверка: скрипт должен реализовать execute()…» (`script_runner.py:355-364`). Это корректно по смыслу (такой скрипт пишет в `product_ff` сам), но формулировка вводит в заблуждение — предлагаю заодно сделать её статус-нейтральной: «Проверка без сохранения: …» (затрагивает также сообщения run/run-all для TEST — тексты, не логику).

### 3. `main.py` — эндпоинт и оркестрация

**a. `_prerequisites_satisfied` (строка 895): добавить необязательный параметр** `extra_passed: Optional[set[str]] = None` — код считается выполненным, если `product_has_actual_ff_pass(...)` **или** он есть в `extra_passed` (успехи текущего вызова). Расширение обратно совместимо (используется в `run_one` и `run_all`).

**b. Новый эндпоинт** (рядом с `run_all`, после него):

```python
@app.post("/api/v1/run-all-workspace", responses=_openapi_client_errors(400, 405, 422))
async def run_all_workspace(
    request: Request,
    app: str = Query(..., description="Мнемоника приложения (alias продукта)"),
    branch: Optional[str] = Query(default=None, description=_BRANCH_QUERY_DESCRIPTION),
):
```

Шаги:
1. `app` пустой → 400 (`Поле app не может быть пустым`); `run_branch = _branch_from_query(branch)`.
2. `raw = await request.body()`; пустое тело → 400; `json.loads` → 400 при `JSONDecodeError` (русский текст); результат не `dict` → 400 «Тело запроса должно быть JSON-объектом workspace».
3. `fetch_structurizr_credentials(app)` → 502 при `CredentialsFetchError` (как в `run_all`).
4. `doc_id = upload_workspace_json(raw, ...)`; `DocumentApiError` → 502 с текстом ошибки document-service.
5. Набор кодов — **переиспользовать `_runnable_ff_codes_ordered()`** (строка 921): она уже исключает TEST, объединяет method-проверки и скрипты с диска, дедуплицирует и упорядочивает. Дополнительно: `ff_app_map = get_fitness_function_applicabilities()`, `code_to_id = get_fitness_function_code_to_id()`, `ff_rows = get_all_fitness_functions()`, `excluded = get_fitness_function_codes_excluded_from_run_all()`.
   - Коды, которых нет в БД (только файл на диске), как и в `run-all`, **запускаются**; в ответе у них `"status": null` (в `run-all` для них подставляется `TEST` — здесь это противоречило бы фильтру).
6. Прогон (гибридные раунды, в отличие от `run_all` не по БД, а по памяти):
   - раунд 1 — коды с пустой applicability (`_applicability_empty`);
   - далее цикл: коды с непустой applicability, для которых `_prerequisites_satisfied(app, appl, code_to_id, run_branch, extra_passed=passed_in_call)` → запускаем; накапливаем `passed_in_call` из `check_result["is_check"] is True`. Цикл до отсутствия прогресса — так зависимые проверки получают шанс в пределах одного вызова.
   - асинхронные (`method` и не `method_synchronous`) — не вызываем, сразу в `skipped`;
   - остальные не запущенные — `skipped` с причиной «условия applicability не выполнены…»; отдельно — TEST-коды с причиной «проверка со статусом TEST не входит в расчёт».
7. Ответ 200: `{"app", "branch", "docId", "results", "skipped"}`, где `results[code] = {"success", "message", "check_result", "status"}`; при пустых `results` — ещё и `message` с пояснением (как в `run_all`).

### 4. Что НЕ меняем

`script_runner.py` (кроме возможной правки текста сообщения из п.2b), `scripts/_common.py`, `db.py`, `run_result.py`, `ff_status.py`. Запись в БД для этого режима не нужна вовсе, поэтому новых db-функций не появляется.

## Документация

- **новый** `documentation/ff-manager/api/POST__api_v1_run-all-workspace.md` — по структуре `POST__api_v1_run-all.md` (общее описание, параметры, валидация, модель данных, ответ с примером, очереди, интеграции).
- `documentation/ff-manager/Общее описание сервиса.md` — эндпоинт в перечне + интеграция Documents API (v2, создание документа).
- `README.md` — строка в таблице API, уточнение семантики `FF_DOCUMENTS_API_BASE_URL` (чтение по `docId` **и** создание workspace-документа), новый `FF_DOCUMENTS_API_TIMEOUT_SECONDS`.
- `docs/SCRIPTS.md` и `docs/CUSTOM_SCRIPT_GUIDE.md` — абзац: в новом режиме `data` = workspace из тела, результаты не сохраняются, `docId` в env не передаётся; скрипт, вызывающий `run_check()` сам, всё равно запишет результат (ограничение).

## Проверка

1. `docker compose up --build -d`; в `docker-compose.yml` для проверки задать `FF_DOCUMENTS_API_BASE_URL` и `FF_FDM_PRODUCT_API_BASE_URL` (без них создание документа невозможно → 502).
2. Снять базовые счётчики: `docker compose exec db psql -U ffuser -d ffmanager -c "select count(*) from ff.product_ff; select count(*) from ff.outside_ff;"`.
3. Успешный прогон (образцы workspace лежат рядом):
   ```bash
   curl -s -X POST 'http://localhost:8000/api/v1/run-all-workspace?app=DEMOFF&branch=main' \
     -H 'Content-Type: application/json' \
     --data-binary @../structurizr-backend-new/tests/workspace.json | jq
   ```
   Ожидаем: 200, непустые `docId` и `results` по DEMOFF-1..4, `skipped` с причинами.
4. Негативные: пустое тело → 400; `not json` → 400; `[1,2]` в теле → 400; `?app=` → 400; неверный `branch=` → 400; без `FF_DOCUMENTS_API_BASE_URL` → 502.
5. Главное утверждение фичи — **счётчики `ff.product_ff` и `ff.outside_ff` после прогона не изменились**.
6. `TEST-001` (`data["documentation"]["decisions"]`) — проверить, что `data` реально доезжает: передать workspace с `documentation.decisions` и убедиться, что `check_result.details` непустой.
7. Смоук через `docker compose up --wait` (как в CI) не затрагивается: новый эндпоинт никто не вызывает автоматически.
