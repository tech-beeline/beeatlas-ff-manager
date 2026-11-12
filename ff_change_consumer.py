# Copyright (c) 2024 PJSC VimpelCom
"""Слушатель очереди ff_change: CREATE и UPDATE fitness function запускают перерасчёт."""
import json
import logging
import threading
import time

import requests

from config import settings
from db import get_fitness_function_by_id

log = logging.getLogger(__name__)

_QUEUE = "ff_change"
_EXCHANGE = "Fitness_function"


def is_test_status(status) -> bool:
    if status is None:
        return False
    return str(status).strip().lower() == "test"


def start_ff_change_listener() -> None:
    thread = threading.Thread(target=_consume_forever, name="ff-change", daemon=True)
    thread.start()


def _consume_forever() -> None:
    while True:
        try:
            import pika

            credentials = pika.PlainCredentials(settings.rabbit_user, settings.rabbit_password)
            params = pika.ConnectionParameters(
                host=settings.rabbit_host,
                port=settings.rabbit_port,
                virtual_host=settings.rabbit_virtual_host,
                credentials=credentials,
            )
            connection = pika.BlockingConnection(params)
            channel = connection.channel()
            channel.exchange_declare(exchange=_EXCHANGE, exchange_type="fanout", durable=True)
            channel.queue_declare(queue=_QUEUE, durable=True)
            channel.queue_bind(queue=_QUEUE, exchange=_EXCHANGE)
            channel.basic_qos(prefetch_count=1)
            channel.basic_consume(queue=_QUEUE, on_message_callback=_on_message, auto_ack=False)
            log.info("ff_change listener started")
            channel.start_consuming()
        except Exception:
            log.exception("ff_change listener stopped, retry in 5s")
            time.sleep(5)


def _on_message(channel, method, _properties, body) -> None:
    try:
        _handle(body)
    except Exception:
        log.exception("ff_change: ошибка обработки сообщения")
    finally:
        channel.basic_ack(delivery_tag=method.delivery_tag)


def _handle(body) -> None:
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        log.error("ff_change: тело не JSON")
        return
    if not isinstance(payload, dict) or "id" not in payload or "changeType" not in payload:
        log.error("ff_change: нет id или changeType")
        return
    change_type = payload.get("changeType")
    if change_type not in ("CREATE", "UPDATE"):
        log.info("ff_change: changeType %s пропущен", change_type)
        return
    try:
        ff_id = int(payload["id"])
    except (TypeError, ValueError):
        log.error("ff_change: id не число")
        return
    row = get_fitness_function_by_id(ff_id)
    if row is None:
        log.error("ff_change: fitness function %s не найдена", ff_id)
        return
    code = (row.get("code") or "").strip()
    if not code:
        log.error("ff_change: у fitness function %s пустой code", ff_id)
        return
    if is_test_status(row.get("status")):
        log.info("ff_change: fitness function %s в статусе TEST, перерасчёт не стартует", code)
        return
    _start_recalc(code)


def _start_recalc(code: str) -> None:
    base = (settings.bpm_url or "").rstrip("/")
    if not base:
        log.error("ff_change: FF_BPM_URL не задан, перерасчёт %s не стартует", code)
        return
    url = f"{base}/engine-rest/process-definition/key/Process_recalc_ff/start"
    body = {
        "businessKey": code,
        "variables": {"ffCode": {"value": code, "type": "String"}},
    }
    try:
        response = requests.post(url, json=body, timeout=30)
    except requests.RequestException as exc:
        log.error("ff_change: ошибка старта перерасчёта %s: %s", code, exc)
        return
    if not response.ok:
        log.error("ff_change: старт перерасчёта %s вернул %s", code, response.status_code)
