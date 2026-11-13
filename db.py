# Copyright (c) 2024 PJSC VimpelCom
"""Подключение к PostgreSQL и инициализация схемы ff."""
import json
from datetime import datetime
from typing import Any, Optional

import psycopg2
from psycopg2.extras import RealDictCursor
from contextlib import contextmanager

from config import settings
from ff_status import (
    FF_STATUS_ADOPT,
    FF_STATUS_TEST,
    FF_STATUS_TRIAL,
    excluded_from_run_all,
    normalize_ff_status,
    should_clear_ff_run_data,
    skips_product_ff_persistence,
)
from branch import DEFAULT_BRANCH
from run_result import resolve_detail_fields_for_storage

SCHEMA = "ff"


def _server_now() -> datetime:
    """Текущее время сервера приложения для записи в БД."""
    return datetime.now()


def _ff_code_equals_sql(column: str = "code") -> str:
    return f"LOWER(BTRIM({column})) = LOWER(BTRIM(%s))"


def _row_ff_status(row: dict) -> str:
    """Статус из строки БД (колонка status)."""
    raw = row.get("status")
    if raw is not None and str(raw).strip():
        return normalize_ff_status(str(raw))
    return FF_STATUS_TEST


def fitness_function_status_from_row(row: Optional[dict]) -> str:
    """Публичная обёртка для нормализации status в ответах API."""
    if row is None:
        return FF_STATUS_TEST
    return _row_ff_status(row)


def get_connection():
    return psycopg2.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        dbname=settings.db_name,
    )


@contextmanager
def get_cursor():
    conn = get_connection()
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_latest_check_result(product_code: str, ff_code: str, branch: str = DEFAULT_BRANCH) -> Optional[bool]:
    """
    Возвращает is_check последней актуальной записи product_ff для пары (код продукта, фитнес-функция по code).
    Если записи нет — None.
    """
    payload = get_latest_check_result_payload(product_code, ff_code, branch)
    if payload is None:
        return None
    return payload.get("is_check")


def get_latest_check_result_payload(
    product_code: str,
    ff_code: str,
    branch: str = DEFAULT_BRANCH,
) -> Optional[dict]:
    """Последняя актуальная запись product_ff для ответа run (is_check, json_details, счётчики)."""
    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT pf.is_check, pf.json_details, pf.count_detail, pf.success_detail
            FROM {SCHEMA}.product_ff pf
            JOIN {SCHEMA}.fitness_function ff ON ff.id = pf.ff_id
            WHERE LOWER(pf.product_code) = LOWER(%s) AND LOWER(pf.branch) = LOWER(%s)
              AND ff.code = %s AND pf.is_actual = true
            ORDER BY pf.create_date DESC
            LIMIT 1
            """,
            (product_code.strip(), branch, ff_code.strip()),
        )
        row = cur.fetchone()
    return dict(row) if row is not None else None


def get_fitness_function_code_to_id() -> dict[str, int]:
    """Словарь code -> id для всех fitness_function."""
    with get_cursor() as cur:
        cur.execute(f"SELECT id, code FROM {SCHEMA}.fitness_function")
        rows = cur.fetchall()
    return {r["code"]: r["id"] for r in rows}


def get_fitness_function_status(*, ff_id: Optional[int] = None, ff_code: Optional[str] = None) -> Optional[str]:
    """Статус проверки (TEST / TRIAL / ADOPT) или None, если не найдена."""
    if ff_id is None and ff_code is None:
        return None
    with get_cursor() as cur:
        if ff_id is not None:
            cur.execute(
                f"SELECT status FROM {SCHEMA}.fitness_function WHERE id = %s",
                (ff_id,),
            )
        else:
            cur.execute(
                f"SELECT status FROM {SCHEMA}.fitness_function WHERE code = %s",
                (ff_code.strip(),),
            )
        row = cur.fetchone()
    if row is None:
        return None
    return _row_ff_status(dict(row))


def save_product_ff_result(
    product_code: str,
    ff_code: str,
    is_check: bool,
    json_details: Optional[str] = None,
    count_detail: Optional[int] = None,
    success_detail: Optional[int] = None,
    source_type: Optional[str] = None,
    source_id: Optional[str] = None,
    branch: str = DEFAULT_BRANCH,
) -> tuple[str, Optional[int]]:
    """
    Добавляет актуальную запись product_ff для продукта (внешний код), ветки и проверки (ff code).
    Предыдущие актуальные записи для этой тройки помечаются is_actual = false.
    Возвращает ("ok", id), ("fitness_function_not_found", None) или ("test_mode", None) для status=TEST.
    """
    code_key = product_code.strip()
    code = ff_code.strip()
    with get_cursor() as cur:
        cur.execute(
            f"SELECT id, status FROM {SCHEMA}.fitness_function WHERE {_ff_code_equals_sql()}",
            (code,),
        )
        row = cur.fetchone()
        if not row:
            return ("fitness_function_not_found", None)
        if skips_product_ff_persistence(_row_ff_status(dict(row))):
            return ("test_mode", None)
        ff_id = row["id"]

        json_details, count_detail, success_detail = resolve_detail_fields_for_storage(
            json_details=json_details,
            count_detail=count_detail,
            success_detail=success_detail,
        )

        new_id = _insert_actual_product_ff(
            cur,
            product_code=code_key,
            branch=branch,
            ff_id=ff_id,
            is_check=is_check,
            json_details=json_details,
            count_detail=count_detail,
            success_detail=success_detail,
            source_type=source_type,
            source_id=source_id,
        )
        return ("ok", new_id)


def _insert_actual_product_ff(
    cur,
    *,
    product_code: str,
    branch: str,
    ff_id: int,
    is_check: bool,
    json_details: Optional[str],
    count_detail: Optional[int],
    success_detail: Optional[int],
    source_type: Optional[str],
    source_id: Optional[str],
) -> int:
    cur.execute(
        f"""
        UPDATE {SCHEMA}.product_ff
        SET is_actual = false
        WHERE LOWER(product_code) = LOWER(%s) AND LOWER(branch) = LOWER(%s) AND ff_id = %s AND is_actual = true
        """,
        (product_code, branch, ff_id),
    )
    cur.execute(
        f"""
        INSERT INTO {SCHEMA}.product_ff (
            product_code,
            branch,
            ff_id,
            is_check,
            is_actual,
            json_details,
            count_detail,
            success_detail,
            source_type,
            source_id,
            create_date
        )
        VALUES (%s, %s, %s, %s, true, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            product_code,
            branch,
            ff_id,
            is_check,
            json_details,
            count_detail,
            success_detail,
            source_type,
            source_id,
            _server_now(),
        ),
    )
    return cur.fetchone()["id"]


def set_fitness_function_status(code: str, new_status: str) -> Optional[dict]:
    """
    Устанавливает статус TEST / TRIAL / ADOPT.
    При смене с/на TEST очищает product_ff и outside_ff для проверки.
    """
    key = code.strip()
    status_norm = normalize_ff_status(new_status)
    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT id, status FROM {SCHEMA}.fitness_function
            WHERE {_ff_code_equals_sql()}
            """,
            (key,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        ff_id = row["id"]
        old_status = _row_ff_status(dict(row))
        if should_clear_ff_run_data(old_status, status_norm):
            cur.execute(
                f"DELETE FROM {SCHEMA}.product_ff WHERE ff_id = %s",
                (ff_id,),
            )
            cur.execute(
                f"DELETE FROM {SCHEMA}.outside_ff WHERE ff_id = %s",
                (ff_id,),
            )
        cur.execute(
            f"""
            UPDATE {SCHEMA}.fitness_function
            SET status = %s, updated_date = %s
            WHERE id = %s
            """,
            (status_norm, _server_now(), ff_id),
        )
    return get_fitness_function_by_code(key)


def get_fitness_function_by_code(code: str) -> Optional[dict]:
    """Одна строка fitness_function по code или None."""
    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT id, code, description, applicability, auxiliary_check, status, script, method,
                   method_synchronous, created_date, updated_date
            FROM {SCHEMA}.fitness_function
            WHERE {_ff_code_equals_sql()}
            """,
            (code.strip(),),
        )
        row = cur.fetchone()
        if row is None:
            return None
        d = dict(row)
        d["status"] = _row_ff_status(d)
        return d


def get_fitness_function_by_id(ff_id: int) -> Optional[dict]:
    """code и status строки fitness_function по id или None."""
    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT id, code, status
            FROM {SCHEMA}.fitness_function
            WHERE id = %s
            """,
            (ff_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        d = dict(row)
        d["status"] = _row_ff_status(d)
        return d


def insert_outside_ff_call(
    ff_id: int,
    product_code: str,
    call_id: str,
    *,
    source_type: Optional[str] = None,
    source_id: Optional[str] = None,
    branch: str = DEFAULT_BRANCH,
) -> int:
    """Статус начального вызова — 'call'. Возвращает id строки outside_ff."""
    with get_cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {SCHEMA}.outside_ff (
                ff_id, product_code, branch, call_id, status, source_type, source_id
            )
            VALUES (%s, %s, %s, %s, 'call', %s, %s)
            RETURNING id
            """,
            (ff_id, product_code.strip(), branch, call_id.strip(), source_type, source_id),
        )
        return cur.fetchone()["id"]


def delete_outside_ff_by_call_id(call_id: str) -> None:
    with get_cursor() as cur:
        cur.execute(
            f"DELETE FROM {SCHEMA}.outside_ff WHERE call_id = %s",
            (call_id.strip(),),
        )


def json_details_value_to_db_str(value: Any) -> Optional[str]:
    """Нормализация поля details из JSON тела (webhook / синхронный HTTP) в строку для product_ff."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def process_ff_webhook(
    call_id: str,
    is_check: bool,
    json_details: Optional[str] = None,
    count_detail: Optional[int] = None,
    success_detail: Optional[int] = None,
) -> str:
    """
    Ищет outside_ff по call_id.
    Возвращает: not_found | already_done | ok
    """
    cid = call_id.strip()
    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT id, ff_id, product_code, branch, status, source_type, source_id
            FROM {SCHEMA}.outside_ff
            WHERE call_id = %s
            FOR UPDATE
            """,
            (cid,),
        )
        row = cur.fetchone()
        if row is None:
            return "not_found"
        if row["status"] == "done":
            return "already_done"

        ff_id = row["ff_id"]
        pc = row["product_code"]
        source_type = row.get("source_type")
        source_id = row.get("source_id")

        cur.execute(
            f"SELECT status FROM {SCHEMA}.fitness_function WHERE id = %s",
            (ff_id,),
        )
        ff_row = cur.fetchone()
        ff_status = _row_ff_status(dict(ff_row)) if ff_row else FF_STATUS_ADOPT
        is_test_mode = skips_product_ff_persistence(ff_status)

        json_details, count_detail, success_detail = resolve_detail_fields_for_storage(
            json_details=json_details,
            count_detail=count_detail,
            success_detail=success_detail,
        )

        if not is_test_mode:
            _insert_actual_product_ff(
                cur,
                product_code=pc,
                branch=row.get("branch") or DEFAULT_BRANCH,
                ff_id=ff_id,
                is_check=is_check,
                json_details=json_details,
                count_detail=count_detail,
                success_detail=success_detail,
                source_type=source_type,
                source_id=source_id,
            )
        if is_test_mode:
            cur.execute(
                f"""
                UPDATE {SCHEMA}.outside_ff
                SET status = 'done',
                    is_check = %s,
                    json_details = %s,
                    count_detail = %s,
                    success_detail = %s
                WHERE id = %s
                """,
                (is_check, json_details, count_detail, success_detail, row["id"]),
            )
        else:
            cur.execute(
                f"""
                UPDATE {SCHEMA}.outside_ff
                SET status = 'done'
                WHERE id = %s
                """,
                (row["id"],),
            )
        return "test_ok" if is_test_mode else "ok"


def get_outside_ff_call(call_id: str) -> Optional[dict]:
    """Запись outside_ff с кодом проверки и статусом ФФ (для опроса асинхронного тестового вызова)."""
    cid = call_id.strip()
    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT o.id, o.ff_id, o.product_code, o.branch, o.call_id, o.status,
                   o.is_check, o.json_details, o.count_detail, o.success_detail,
                   ff.code AS ff_code, ff.description AS ff_name, ff.status AS ff_status
            FROM {SCHEMA}.outside_ff o
            JOIN {SCHEMA}.fitness_function ff ON ff.id = o.ff_id
            WHERE o.call_id = %s
            """,
            (cid,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        d = dict(row)
        raw = d.get("ff_status")
        d["ff_status"] = _row_ff_status({"status": raw})
        return d


def product_has_actual_ff_pass(product_code: str, ff_id: int, branch: str = DEFAULT_BRANCH) -> bool:
    """
    True, если для (product_code, branch, ff_id) есть актуальная запись product_ff с is_check = true.
    Сравнение product_code и branch регистронезависимое.
    """
    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT 1
            FROM {SCHEMA}.product_ff
            WHERE LOWER(product_code) = LOWER(%s) AND LOWER(branch) = LOWER(%s)
              AND ff_id = %s AND is_actual = true AND is_check = true
            LIMIT 1
            """,
            (product_code.strip(), branch, ff_id),
        )
        return cur.fetchone() is not None


def get_fitness_function_applicabilities() -> dict:
    """
    Возвращает словарь {code: applicability} для всех fitness_function.
    """
    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT code, applicability
            FROM {SCHEMA}.fitness_function
            """
        )
        rows = cur.fetchall()
    return {r["code"]: r.get("applicability") for r in rows}


def add_fitness_function(
    code: str,
    description: str,
    applicability: Optional[str],
    auxiliary_check: bool = False,
    status: str = FF_STATUS_TEST,
    script: Optional[str] = None,
    set_script: bool = False,
    method: Optional[str] = None,
    set_method: bool = False,
    method_synchronous: bool = False,
    set_method_synchronous: bool = False,
    create_only: bool = False,
) -> Optional[int]:
    """
    Добавляет новую фитнес-функцию (если такой code ещё нет) и возвращает её id.
    Если запись с таким code уже существует — при create_only=False обновляет поля и возвращает id;
    при create_only=True не меняет строку и возвращает None.
    status: TEST | TRIAL | ADOPT.
    При смене статуса с/на TEST удаляются product_ff и outside_ff для этой функции.
    """
    status_norm = normalize_ff_status(status)
    method_value: Optional[str] = None
    if set_method:
        method_value = method

    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT id, status FROM {SCHEMA}.fitness_function
            WHERE {_ff_code_equals_sql()}
            """,
            (code,),
        )
        row = cur.fetchone()
        if row:
            if create_only:
                return None
            ff_id = row["id"]
            old_status = _row_ff_status(dict(row))
            if should_clear_ff_run_data(old_status, status_norm):
                cur.execute(
                    f"DELETE FROM {SCHEMA}.product_ff WHERE ff_id = %s",
                    (ff_id,),
                )
                cur.execute(
                    f"DELETE FROM {SCHEMA}.outside_ff WHERE ff_id = %s",
                    (ff_id,),
                )
            sets = [
                "description = %s",
                "applicability = %s",
                "auxiliary_check = %s",
                "status = %s",
            ]
            params: list = [description, applicability, auxiliary_check, status_norm]
            if set_script:
                sets.append("script = %s")
                params.append(script)
            if set_method:
                sets.append("method = %s")
                params.append(method_value)
            if set_method_synchronous:
                sets.append("method_synchronous = %s")
                params.append(method_synchronous)
            sets.append("updated_date = %s")
            params.append(_server_now())
            params.append(ff_id)
            cur.execute(
                f"""
                UPDATE {SCHEMA}.fitness_function
                SET {", ".join(sets)}
                WHERE id = %s
                """,
                params,
            )
            return ff_id

        ms_val = method_synchronous if set_method_synchronous else False
        cur.execute(
            f"""
            INSERT INTO {SCHEMA}.fitness_function
                (code, description, applicability, auxiliary_check, status, script, method,
                 method_synchronous, created_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                code,
                description,
                applicability,
                auxiliary_check,
                status_norm,
                script if set_script else None,
                method_value if set_method else None,
                ms_val,
                _server_now(),
            ),
        )
        new_row = cur.fetchone()
        return new_row["id"]


def get_all_fitness_functions() -> list[dict]:
    """Возвращает список всех fitness_function (включая вспомогательные — для админки)."""
    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT id, code, description, applicability, auxiliary_check, status, script, method,
                   method_synchronous, created_date, updated_date
            FROM {SCHEMA}.fitness_function
            ORDER BY id
            """
        )
        rows = cur.fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["status"] = _row_ff_status(d)
        out.append(d)
    return out


def get_fitness_function_codes_excluded_from_run_all() -> set[str]:
    """Коды проверок со статусом TEST (не входят в run-all)."""
    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT code, status FROM {SCHEMA}.fitness_function
            """
        )
        rows = cur.fetchall()
    return {
        r["code"]
        for r in rows
        if r.get("code") and excluded_from_run_all(_row_ff_status(dict(r)))
    }


def get_published_fitness_functions() -> list[dict]:
    """
    Справочник опубликованных основных проверок для дашборда:
    auxiliary_check = false, status in (TRIAL, ADOPT), сортировка по id.
    """
    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT id, code, description, status
            FROM {SCHEMA}.fitness_function
            WHERE auxiliary_check IS NOT TRUE
              AND status IN (%s, %s)
            ORDER BY id
            """,
            (FF_STATUS_TRIAL, FF_STATUS_ADOPT),
        )
        rows = cur.fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["status"] = _row_ff_status(d)
        out.append(d)
    return out


def get_actual_results_by_product_codes(product_codes: list[str]) -> list[dict]:
    """
    Актуальные product_ff ветки main для нескольких продуктов одним запросом.
    Те же фильтры, что у get_actual_results_by_product_code (основные проверки, TRIAL/ADOPT).
    Сравнение product_code без учёта регистра.
    """
    codes = [c.strip() for c in product_codes if c and str(c).strip()]
    if not codes:
        return []
    lowered = [c.lower() for c in codes]
    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT
                pf.id,
                pf.product_code,
                pf.branch,
                pf.ff_id,
                pf.is_check,
                pf.create_date,
                pf.json_details,
                pf.count_detail,
                pf.success_detail,
                pf.source_type,
                pf.source_id,
                ff.code AS ff_code,
                ff.description AS ff_description,
                ff.status AS ff_status
            FROM {SCHEMA}.product_ff pf
            JOIN {SCHEMA}.fitness_function ff ON ff.id = pf.ff_id
            WHERE pf.is_actual = true
              AND LOWER(pf.product_code) = ANY(%s)
              AND LOWER(pf.branch) = %s
              AND (ff.auxiliary_check IS NOT TRUE)
              AND (ff.status IN (%s, %s))
            ORDER BY pf.product_code, pf.create_date
            """,
            (lowered, DEFAULT_BRANCH, FF_STATUS_TRIAL, FF_STATUS_ADOPT),
        )
        rows = cur.fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["ff_status"] = _row_ff_status({"status": d.get("ff_status")})
        out.append(d)
    return out


def get_actual_results_by_product_code(
    product_code: str,
    *,
    branch: str = DEFAULT_BRANCH,
    auxiliary_only: bool = False,
) -> list[dict]:
    """
    По коду продукта (внешняя мнемоника) и ветке возвращает актуальные записи product_ff:
    - auxiliary_only=False (по умолчанию): основные проверки (auxiliary_check не true);
    - auxiliary_only=True: только вспомогательные (auxiliary_check = true).
    В обоих случаях не включается status = TEST (только TRIAL и ADOPT).
    Поля: id, product_code, ff_id, ff_code, ff_description, is_check, create_date,
    json_details, count_detail, success_detail, source_type, source_id.
    Если записей нет — пустой список.
    Сравнение кода продукта без учёта регистра.
    """
    code = product_code.strip()
    aux_clause = (
        "(ff.auxiliary_check IS TRUE)"
        if auxiliary_only
        else "(ff.auxiliary_check IS NOT TRUE)"
    )
    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT
                pf.id,
                pf.product_code,
                pf.branch,
                pf.ff_id,
                pf.is_check,
                pf.create_date,
                pf.json_details,
                pf.count_detail,
                pf.success_detail,
                pf.source_type,
                pf.source_id,
                ff.code AS ff_code,
                ff.description AS ff_description,
                ff.status AS ff_status
            FROM {SCHEMA}.product_ff pf
            JOIN {SCHEMA}.fitness_function ff ON ff.id = pf.ff_id
            WHERE LOWER(pf.product_code) = LOWER(%s) AND LOWER(pf.branch) = LOWER(%s) AND pf.is_actual = true
              AND {aux_clause}
              AND (ff.status IN (%s, %s))
            ORDER BY pf.create_date
            """,
            (code, branch, FF_STATUS_TRIAL, FF_STATUS_ADOPT),
        )
        rows = cur.fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["ff_status"] = _row_ff_status({"status": d.get("ff_status")})
        out.append(d)
    return out


def get_product_ff_history(
    product_code: str,
    ff_code: str,
    branch: str = DEFAULT_BRANCH,
) -> Optional[list[dict]]:
    """
    История всех записей product_ff для (код продукта, ветка, код проверки), от новых к старым.
    None — если проверка с ff_code не найдена в fitness_function.
    Поля: id, product_code, ff_id, ff_code, ff_description, is_check, is_actual, create_date,
    json_details, count_detail, success_detail, source_type, source_id, ff_status (текущий статус проверки).
    """
    pc = product_code.strip()
    fc = ff_code.strip()
    with get_cursor() as cur:
        cur.execute(
            f"SELECT id FROM {SCHEMA}.fitness_function WHERE code = %s",
            (fc,),
        )
        if cur.fetchone() is None:
            return None
        cur.execute(
            f"""
            SELECT
                pf.id,
                pf.product_code,
                pf.branch,
                pf.ff_id,
                pf.is_check,
                pf.is_actual,
                pf.create_date,
                pf.json_details,
                pf.count_detail,
                pf.success_detail,
                pf.source_type,
                pf.source_id,
                ff.code AS ff_code,
                ff.description AS ff_description,
                ff.status AS ff_status
            FROM {SCHEMA}.product_ff pf
            JOIN {SCHEMA}.fitness_function ff ON ff.id = pf.ff_id
            WHERE LOWER(pf.product_code) = LOWER(%s) AND LOWER(pf.branch) = LOWER(%s) AND ff.code = %s
            ORDER BY pf.create_date DESC, pf.id DESC
            """,
            (pc, branch, fc),
        )
        rows = cur.fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["ff_status"] = _row_ff_status({"status": d.get("ff_status")})
        out.append(d)
    return out


def find_missing_ff_code(unique_ff_codes: list[str]) -> Optional[str]:
    """
    Проверяет существование каждого кода в fitness_function (case-insensitive).
    Возвращает первый отсутствующий код (в порядке unique_ff_codes) или None.
    """
    if not unique_ff_codes:
        return None
    lower_input = [c.lower() for c in unique_ff_codes]
    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT LOWER(BTRIM(code)) AS lower_code
            FROM {SCHEMA}.fitness_function
            WHERE LOWER(BTRIM(code)) = ANY(%s)
            """,
            (lower_input,),
        )
        rows = cur.fetchall()
    found = {r["lower_code"] for r in rows}
    for code in unique_ff_codes:
        if code.lower() not in found:
            return code
    return None


def get_products_passed_ff_codes(unique_ff_codes: list[str]) -> list[str]:
    """
    Возвращает product_code продуктов, у которых есть актуальная пройденная запись
    в product_ff по каждому из переданных кодов (AND-логика).
    Условия: is_actual=true, is_check=true, ff.status IN (TRIAL, ADOPT).
    Результат отсортирован по product_code без учёта регистра.
    """
    lower_codes = [c.lower() for c in unique_ff_codes]
    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT pf.product_code
            FROM {SCHEMA}.product_ff pf
            JOIN {SCHEMA}.fitness_function ff ON ff.id = pf.ff_id
            WHERE pf.is_actual = true
              AND pf.is_check = true
              AND LOWER(pf.branch) = %s
              AND ff.status IN (%s, %s)
              AND LOWER(BTRIM(ff.code)) = ANY(%s)
            GROUP BY pf.product_code
            HAVING COUNT(DISTINCT LOWER(BTRIM(ff.code))) = %s
            ORDER BY LOWER(pf.product_code)
            """,
            (DEFAULT_BRANCH, FF_STATUS_TRIAL, FF_STATUS_ADOPT, lower_codes, len(lower_codes)),
        )
        rows = cur.fetchall()
    return [r["product_code"] for r in rows]


def init_schema():
    """Создание схемы ff, таблиц и тестовых данных при первом старте."""
    conn = get_connection()
    conn.autocommit = True
    cur = conn.cursor()
    try:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA};")

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA}.fitness_function (
                id SERIAL PRIMARY KEY,
                code TEXT,
                description TEXT,
                applicability TEXT,
                auxiliary_check BOOLEAN NOT NULL DEFAULT false,
                test BOOLEAN NOT NULL DEFAULT false,
                script TEXT,
                method TEXT
            );
        """)

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA}.outside_ff (
                id SERIAL PRIMARY KEY,
                ff_id INT NOT NULL REFERENCES {SCHEMA}.fitness_function(id) ON DELETE CASCADE,
                product_code TEXT NOT NULL,
                call_id TEXT NOT NULL,
                status TEXT NOT NULL,
                CONSTRAINT outside_ff_call_id_unique UNIQUE (call_id)
            );
        """)

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA}.product_ff (
                id SERIAL PRIMARY KEY,
                product_code TEXT NOT NULL,
                ff_id INT NOT NULL REFERENCES {SCHEMA}.fitness_function(id),
                is_check BOOLEAN NOT NULL,
                is_actual BOOLEAN NOT NULL DEFAULT false,
                json_details TEXT,
                count_detail INT,
                success_detail INT,
                create_date TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        """)

        cur.execute(f"""
            ALTER TABLE {SCHEMA}.product_ff
            ADD COLUMN IF NOT EXISTS is_actual BOOLEAN NOT NULL DEFAULT false;
        """)
        cur.execute(f"""
            ALTER TABLE {SCHEMA}.product_ff
            ADD COLUMN IF NOT EXISTS json_details TEXT;
        """)
        cur.execute(f"""
            ALTER TABLE {SCHEMA}.product_ff
            ADD COLUMN IF NOT EXISTS count_detail INT;
        """)
        cur.execute(f"""
            ALTER TABLE {SCHEMA}.product_ff
            ADD COLUMN IF NOT EXISTS success_detail INT;
        """)

        cur.execute(f"""
            DO $$
            BEGIN
              IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = '{SCHEMA}' AND table_name = 'product_ff' AND column_name = 'product_id'
              ) THEN
                ALTER TABLE {SCHEMA}.product_ff ADD COLUMN IF NOT EXISTS product_code TEXT;
                UPDATE {SCHEMA}.product_ff pf
                SET product_code = COALESCE(
                  (SELECT p.alias FROM {SCHEMA}.product p WHERE p.id = pf.product_id),
                  'LEGACY-' || pf.product_id::text
                )
                WHERE pf.product_code IS NULL;
                ALTER TABLE {SCHEMA}.product_ff DROP CONSTRAINT IF EXISTS product_ff_product_id_fkey;
                ALTER TABLE {SCHEMA}.product_ff DROP COLUMN product_id;
                ALTER TABLE {SCHEMA}.product_ff ALTER COLUMN product_code SET NOT NULL;
              END IF;
            END $$;
        """)

        cur.execute(f"""
            ALTER TABLE {SCHEMA}.fitness_function
            ADD COLUMN IF NOT EXISTS applicability TEXT;
        """)
        cur.execute(f"""
            ALTER TABLE {SCHEMA}.fitness_function
            ADD COLUMN IF NOT EXISTS auxiliary_check BOOLEAN NOT NULL DEFAULT false;
        """)
        cur.execute(f"""
            ALTER TABLE {SCHEMA}.fitness_function
            ADD COLUMN IF NOT EXISTS test BOOLEAN NOT NULL DEFAULT false;
        """)
        cur.execute(f"""
            ALTER TABLE {SCHEMA}.fitness_function
            ADD COLUMN IF NOT EXISTS script TEXT;
        """)
        cur.execute(f"""
            ALTER TABLE {SCHEMA}.fitness_function
            ADD COLUMN IF NOT EXISTS method TEXT;
        """)
        cur.execute(f"""
            ALTER TABLE {SCHEMA}.fitness_function
            ADD COLUMN IF NOT EXISTS method_synchronous BOOLEAN NOT NULL DEFAULT false;
        """)
        cur.execute(f"""
            ALTER TABLE {SCHEMA}.fitness_function
            ADD COLUMN IF NOT EXISTS created_date timestamp without time zone NOT NULL DEFAULT CURRENT_TIMESTAMP;
        """)
        cur.execute(f"""
            ALTER TABLE {SCHEMA}.fitness_function
            ADD COLUMN IF NOT EXISTS updated_date timestamp without time zone;
        """)
        # Уникальность code: инвариант справочника ФФ, на нём же строится поиск по коду.
        cur.execute(f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'uq_fitness_function_code'
                ) THEN
                    ALTER TABLE {SCHEMA}.fitness_function ADD CONSTRAINT uq_fitness_function_code UNIQUE (code);
                END IF;
            END $$;
        """)
        cur.execute(f"""
            ALTER TABLE {SCHEMA}.outside_ff
            ADD COLUMN IF NOT EXISTS is_check BOOLEAN;
        """)
        cur.execute(f"""
            ALTER TABLE {SCHEMA}.outside_ff
            ADD COLUMN IF NOT EXISTS json_details TEXT;
        """)
        cur.execute(f"""
            ALTER TABLE {SCHEMA}.outside_ff
            ADD COLUMN IF NOT EXISTS count_detail INT;
        """)
        cur.execute(f"""
            ALTER TABLE {SCHEMA}.outside_ff
            ADD COLUMN IF NOT EXISTS success_detail INT;
        """)
        cur.execute(f"""
            ALTER TABLE {SCHEMA}.product_ff
            ADD COLUMN IF NOT EXISTS source_type TEXT;
        """)
        cur.execute(f"""
            ALTER TABLE {SCHEMA}.product_ff
            ADD COLUMN IF NOT EXISTS source_id TEXT;
        """)
        cur.execute(f"""
            ALTER TABLE {SCHEMA}.outside_ff
            ADD COLUMN IF NOT EXISTS source_type TEXT;
        """)
        cur.execute(f"""
            ALTER TABLE {SCHEMA}.outside_ff
            ADD COLUMN IF NOT EXISTS source_id TEXT;
        """)
        cur.execute(f"""
            ALTER TABLE {SCHEMA}.product_ff
            ADD COLUMN IF NOT EXISTS branch TEXT NOT NULL DEFAULT '{DEFAULT_BRANCH}';
        """)
        cur.execute(f"""
            ALTER TABLE {SCHEMA}.outside_ff
            ADD COLUMN IF NOT EXISTS branch TEXT NOT NULL DEFAULT '{DEFAULT_BRANCH}';
        """)
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_product_ff_actual_product_branch
                ON {SCHEMA}.product_ff (LOWER(product_code), LOWER(branch), ff_id)
                WHERE is_actual = true;
        """)
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA}.schema_meta (
                key TEXT PRIMARY KEY
            );
            """
        )
        cur.execute(f"""
            ALTER TABLE {SCHEMA}.fitness_function
            ADD COLUMN IF NOT EXISTS status TEXT;
        """)
        cur.execute(
            f"""
            WITH ins AS (
                INSERT INTO {SCHEMA}.schema_meta (key)
                SELECT 'fitness_function_status_from_test'
                WHERE NOT EXISTS (
                    SELECT 1 FROM {SCHEMA}.schema_meta
                    WHERE key = 'fitness_function_status_from_test'
                )
                RETURNING 1
            )
            UPDATE {SCHEMA}.fitness_function ff
            SET status = CASE WHEN ff.test IS TRUE THEN 'TEST' ELSE 'ADOPT' END
            WHERE ff.status IS NULL
              AND EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = '{SCHEMA}'
                  AND table_name = 'fitness_function'
                  AND column_name = 'test'
              )
              AND EXISTS (SELECT 1 FROM ins);
            """
        )
        cur.execute(
            f"""
            UPDATE {SCHEMA}.fitness_function
            SET status = 'TEST'
            WHERE status IS NULL;
            """
        )
        cur.execute(f"""
            ALTER TABLE {SCHEMA}.fitness_function
            ALTER COLUMN status SET DEFAULT 'TEST';
        """)
        cur.execute(
            f"""
            DO $$
            BEGIN
              IF NOT EXISTS (
                SELECT 1 FROM {SCHEMA}.schema_meta
                WHERE key = 'fitness_function_drop_test_column'
              ) THEN
                ALTER TABLE {SCHEMA}.fitness_function DROP COLUMN IF EXISTS test;
                INSERT INTO {SCHEMA}.schema_meta (key)
                VALUES ('fitness_function_drop_test_column');
              END IF;
            END $$;
            """
        )
        # Одноразово: для уже существовавших проверок с непустым method признак синхронности = false (асинхронный webhook).
        cur.execute(
            f"""
            WITH ins AS (
                INSERT INTO {SCHEMA}.schema_meta (key)
                SELECT 'fitness_function_method_synchronous_async_backfill'
                WHERE NOT EXISTS (
                    SELECT 1 FROM {SCHEMA}.schema_meta
                    WHERE key = 'fitness_function_method_synchronous_async_backfill'
                )
                RETURNING 1
            )
            UPDATE {SCHEMA}.fitness_function
            SET method_synchronous = false
            WHERE method IS NOT NULL
              AND BTRIM(method) <> ''
              AND EXISTS (SELECT 1 FROM ins);
            """
        )

        cur.execute(f"DROP TABLE IF EXISTS {SCHEMA}.product;")

        cur.execute(f"SELECT 1 FROM {SCHEMA}.fitness_function WHERE id = 1;")
        if cur.fetchone() is None:
            cur.execute(f"""
                INSERT INTO {SCHEMA}.fitness_function (id, code, description)
                VALUES (1, 'DEMOFF-1', 'Тестовая фитнес-функция');
            """)
        cur.execute(f"SELECT 1 FROM {SCHEMA}.fitness_function WHERE id = 2;")
        if cur.fetchone() is None:
            cur.execute(f"""
                INSERT INTO {SCHEMA}.fitness_function (id, code, description)
                VALUES (2, 'DEMOFF-2', 'Тестовая фитнес-функция 2');
            """)

        cur.execute(f"SELECT COALESCE(MAX(id), 0) FROM {SCHEMA}.fitness_function;")
        max_ff_id = cur.fetchone()[0]
        cur.execute(
            f"SELECT setval(pg_get_serial_sequence('{SCHEMA}.fitness_function', 'id'), %s);",
            (max_ff_id,),
        )

    finally:
        cur.close()
        conn.close()


BI_DASHBOARDS_SCHEMA = "bi_dashboards"


def init_bi_dashboards_schema():
    """
    Схема bi_dashboards: витрина результатов ФФ для внешних потребителей (Qlik Sense и др.).
    Реплицируется из ff.product_ff триггером trg_product_ff_replicate (AFTER INSERT).
    FK на product.product(alias) и ff.fitness_function(code) намеренно не создаются:
    витрина не должна зависеть от внешних схем (см. корректирующую миграцию в начале).
    """
    conn = get_connection()
    conn.autocommit = True
    cur = conn.cursor()
    try:
        # Корректирующая миграция (SFDM-4109): витрина не должна зависеть от внешних схем
        # (product.product, ff.fitness_function). FK блокировали удаление продуктов и ФФ
        # и требовали раскатанной V0043 в fdm-products. Целостность обеспечивает триггер
        # fn_replicate_product_ff, который пишет только существующие product_code/ff_code.
        # Идёт первой в функции: на уже развёрнутых окружениях таблица существует, и снятие
        # FK не должно зависеть от успеха остальных DDL ниже (autocommit — каждый ALTER
        # фиксируется отдельно).
        cur.execute(f"""
            ALTER TABLE IF EXISTS {BI_DASHBOARDS_SCHEMA}.ff
                DROP CONSTRAINT IF EXISTS fk_bi_dashboards_ff_product_alias;
        """)
        cur.execute(f"""
            ALTER TABLE IF EXISTS {BI_DASHBOARDS_SCHEMA}.ff
                DROP CONSTRAINT IF EXISTS fk_bi_dashboards_ff_code;
        """)

        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {BI_DASHBOARDS_SCHEMA};")
        cur.execute(f"""
            COMMENT ON SCHEMA {BI_DASHBOARDS_SCHEMA} IS
            'BI Хранилище артефактов (результатов фитнес-функций и иных оценок) для потребления сторонними системами (Qlik Sense и др.). Заменяет устаревшую public.fdm_ptr_artifacts.';
        """)

        cur.execute(f"""
            CREATE SEQUENCE IF NOT EXISTS {BI_DASHBOARDS_SCHEMA}.seq_ff_id
                AS BIGINT START 1 INCREMENT 1 NO CYCLE;
        """)

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {BI_DASHBOARDS_SCHEMA}.ff (
                id BIGINT NOT NULL DEFAULT nextval('{BI_DASHBOARDS_SCHEMA}.seq_ff_id'),
                product_alias VARCHAR NOT NULL,
                ff_code VARCHAR(100) NULL,
                is_check BOOLEAN NOT NULL,
                is_actual BOOLEAN NOT NULL DEFAULT true,
                assessed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                details JSONB NULL,
                count_detail INTEGER NULL,
                success_detail INTEGER NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT pk_bi_dashboards_ff PRIMARY KEY (id),
                CONSTRAINT chk_bi_dashboards_ff_counts
                    CHECK (success_detail IS NULL OR count_detail IS NULL OR success_detail <= count_detail)
            );
        """)

        cur.execute(f"""
            COMMENT ON TABLE {BI_DASHBOARDS_SCHEMA}.ff IS
            'Результаты фитнес-функций и иных оценок для систем. Наполняется триггером на ff.product_ff.';
        """)
        cur.execute(f"""
            COMMENT ON COLUMN {BI_DASHBOARDS_SCHEMA}.ff.product_alias IS
            'Ссылка на product.product.alias (CMDB-мнемоника системы).';
        """)
        cur.execute(f"""
            COMMENT ON COLUMN {BI_DASHBOARDS_SCHEMA}.ff.ff_code IS
            'Код фитнес-функции в ff.fitness_function.code.';
        """)
        cur.execute(f"""
            COMMENT ON COLUMN {BI_DASHBOARDS_SCHEMA}.ff.is_check IS
            'Результат: true=pass, false=fail.';
        """)
        cur.execute(f"""
            COMMENT ON COLUMN {BI_DASHBOARDS_SCHEMA}.ff.is_actual IS
            'Актуальная ли запись. При новом результате предыдущие is_actual=false.';
        """)
        cur.execute(f"""
            COMMENT ON COLUMN {BI_DASHBOARDS_SCHEMA}.ff.assessed_at IS
            'Время оценки (бывш. *_ts в fdm_ptr_artifacts).';
        """)
        cur.execute(f"""
            COMMENT ON COLUMN {BI_DASHBOARDS_SCHEMA}.ff.details IS
            'Произвольные метаданные результата (jsonb).';
        """)

        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_bi_dashboards_art_product_alias
                ON {BI_DASHBOARDS_SCHEMA}.ff (product_alias);
        """)
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_bi_dashboards_art_ff_code
                ON {BI_DASHBOARDS_SCHEMA}.ff (ff_code);
        """)
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_bi_dashboards_art_is_actual
                ON {BI_DASHBOARDS_SCHEMA}.ff (is_actual);
        """)
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_bi_dashboards_art_assessed_at
                ON {BI_DASHBOARDS_SCHEMA}.ff (assessed_at);
        """)
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_bi_dashboards_art_product_actual
                ON {BI_DASHBOARDS_SCHEMA}.ff (product_alias, is_actual);
        """)

        # Partial unique index вместо UNIQUE(product_alias, ff_code, is_actual): последнее ограничивало
        # бы и is_actual=false, что ломается начиная с 3-й оценки одной и той же пары (накопление истории).
        # Здесь же ограничивается только "текущая" строка на пару (product_alias, ff_code).
        cur.execute(f"""
            CREATE UNIQUE INDEX IF NOT EXISTS uk_bi_dashboards_ff_actual
                ON {BI_DASHBOARDS_SCHEMA}.ff (product_alias, ff_code)
                WHERE is_actual = true;
        """)

        cur.execute(f"""
            CREATE OR REPLACE FUNCTION {BI_DASHBOARDS_SCHEMA}.fn_set_updated_at()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
              NEW.updated_at = CURRENT_TIMESTAMP;
              RETURN NEW;
            END;
            $$;
        """)
        cur.execute(f"""
            DROP TRIGGER IF EXISTS trg_ff_updated_at ON {BI_DASHBOARDS_SCHEMA}.ff;
        """)
        cur.execute(f"""
            CREATE TRIGGER trg_ff_updated_at
                BEFORE UPDATE ON {BI_DASHBOARDS_SCHEMA}.ff
                FOR EACH ROW EXECUTE FUNCTION {BI_DASHBOARDS_SCHEMA}.fn_set_updated_at();
        """)

        cur.execute(f"""
            CREATE OR REPLACE FUNCTION {BI_DASHBOARDS_SCHEMA}.fn_replicate_product_ff()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            DECLARE
              v_ff_code VARCHAR(100);
            BEGIN
              IF LOWER(NEW.branch) <> '{DEFAULT_BRANCH}' THEN
                RETURN NEW;
              END IF;

              -- 1. Получить код фитнес-функции по ff_id
              SELECT code
                INTO v_ff_code
                FROM {SCHEMA}.fitness_function
               WHERE id = NEW.ff_id;

              IF v_ff_code IS NULL THEN
                -- Фитнес-функция не найдена — пропускаем репликацию
                RETURN NEW;
              END IF;

              -- 2. Снять флаг is_actual с предыдущих актуальных записей
              UPDATE {BI_DASHBOARDS_SCHEMA}.ff
                 SET is_actual = false,
                     updated_at = CURRENT_TIMESTAMP
               WHERE product_alias = NEW.product_code
                 AND ff_code = v_ff_code
                 AND is_actual = true;

              -- 3. Вставить новую актуальную запись
              INSERT INTO {BI_DASHBOARDS_SCHEMA}.ff
                (product_alias, ff_code, is_check, is_actual,
                 assessed_at, details, count_detail, success_detail,
                 created_at, updated_at)
              VALUES
                (NEW.product_code, v_ff_code, NEW.is_check,
                 COALESCE(NEW.is_actual, true),
                 COALESCE(NEW.create_date, CURRENT_TIMESTAMP),
                 NULLIF(NEW.json_details, '')::jsonb, NEW.count_detail, NEW.success_detail,
                 CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);

              RETURN NEW;
            END;
            $$;
        """)
        cur.execute(f"""
            COMMENT ON FUNCTION {BI_DASHBOARDS_SCHEMA}.fn_replicate_product_ff() IS
            'Триггерная функция: реплицирует INSERT из ff.product_ff в bi_dashboards.ff. Срабатывает при записи результата фитнес-функции через ff-manager API.';
        """)

        cur.execute(f"""
            DROP TRIGGER IF EXISTS trg_product_ff_replicate ON {SCHEMA}.product_ff;
        """)
        cur.execute(f"""
            CREATE TRIGGER trg_product_ff_replicate
                AFTER INSERT ON {SCHEMA}.product_ff
                FOR EACH ROW EXECUTE FUNCTION {BI_DASHBOARDS_SCHEMA}.fn_replicate_product_ff();
        """)
        cur.execute(f"""
            COMMENT ON TRIGGER trg_product_ff_replicate ON {SCHEMA}.product_ff IS
            'Репликация результатов фитнес-функций в bi_dashboards.ff для потребления сторонними системами (Qlik Sense).';
        """)
    finally:
        cur.close()
        conn.close()
