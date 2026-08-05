# Copyright (c) 2024 PJSC VimpelCom
"""HTTP-клиент FDM Products API (домены и приложения)."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from config import settings


class ProductApiError(Exception):
    """Ошибка при обращении к FDM Products API."""


def fetch_domain_products() -> dict[str, Any]:
    """
    GET {fdm_product_api_base_url}/api/v1/domain/products
    Ожидается JSON с полем domain — список доменов и вложенных продуктов.
    """
    base = settings.fdm_product_api_base_url.rstrip("/")
    url = f"{base}/api/v1/domain/products"
    req = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=settings.fdm_product_api_timeout_seconds) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise ProductApiError(f"FDM Products API вернуло HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise ProductApiError(f"Не удалось обратиться к FDM Products API: {e}") from e
    except OSError as e:
        raise ProductApiError(f"Ошибка сети при запросе FDM Products API: {e}") from e

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ProductApiError(f"Ответ FDM Products API не является JSON: {e}") from e

    if not isinstance(data, dict):
        raise ProductApiError("Ответ FDM Products API не является JSON-объектом")
    return data
