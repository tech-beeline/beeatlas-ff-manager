# Copyright (c) 2024 PJSC VimpelCom
"""HTTP-клиент document-service: создание workspace-документа."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from config import settings

# Папка и тип документа для workspace.json (Structurizr). В document-service
# пара не зарегистрирована в documentation_type, поэтому targetId передавать нельзя.
WORKSPACE_PATH_NAME = "workspace"
WORKSPACE_DOC_TYPE = "json"
WORKSPACE_FILE_NAME = "workspace.json"


class DocumentApiError(Exception):
    """Ошибка при обращении к document-service."""


def _error_message_from_body(raw: bytes) -> str:
    """document-service отдаёт ошибки как JSON с полем errorMessage."""
    if not raw:
        return ""
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return ""
    if isinstance(payload, dict):
        msg = payload.get("errorMessage") or payload.get("message")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()
    return ""


def upload_workspace_json(
    raw_json: bytes,
    *,
    file_name: str = WORKSPACE_FILE_NAME,
    is_public: bool = True,
) -> int:
    """
    POST {documents_api_base_url}/api/v2/documents/workspace/json?fileName=...&isPublic=...
    Документ создаётся сырым телом запроса (не multipart), Content-Length выставляет urllib.
    Возвращает docId созданного документа; при ошибке бросает DocumentApiError.
    """
    base = (settings.documents_api_base_url or "").strip().rstrip("/")
    if not base:
        raise DocumentApiError(
            "Не задан FF_DOCUMENTS_API_BASE_URL для создания документа workspace"
        )

    query = urllib.parse.urlencode(
        {"fileName": file_name, "isPublic": "true" if is_public else "false"}
    )
    url = (
        f"{base}/api/v2/documents/{WORKSPACE_PATH_NAME}/{WORKSPACE_DOC_TYPE}?{query}"
    )
    req = urllib.request.Request(
        url,
        data=raw_json,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(
            req, timeout=settings.documents_api_timeout_seconds
        ) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = _error_message_from_body(e.read() if e.fp else b"")
        raise DocumentApiError(
            f"Documents API вернул HTTP {e.code}" + (f": {detail}" if detail else "")
        ) from e
    except urllib.error.URLError as e:
        raise DocumentApiError(f"Не удалось обратиться к Documents API: {e}") from e
    except OSError as e:
        raise DocumentApiError(f"Ошибка сети при загрузке документа: {e}") from e

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise DocumentApiError(f"Некорректный JSON от Documents API: {e}") from e
    if not isinstance(payload, dict):
        raise DocumentApiError("Documents API вернул не JSON-объект")

    doc_id = payload.get("docId")
    if isinstance(doc_id, bool) or not isinstance(doc_id, int):
        try:
            doc_id = int(str(doc_id).strip())
        except (TypeError, ValueError):
            raise DocumentApiError("В ответе Documents API отсутствует docId") from None
    return doc_id
