"""Helpers HTTP compartidos por los clientes de proveedores."""

from __future__ import annotations

import logging
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from collections.abc import Callable
from typing import Any

import httpx

logger = logging.getLogger(__name__)
_LAST_STATUS_CODE: ContextVar[int | None] = ContextVar("radar_http_status_code", default=None)


def get_last_status_code() -> int | None:
    """Devuelve el status HTTP de la última llamada en el contexto actual."""
    return _LAST_STATUS_CODE.get()


def _retry_after_seconds(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0, int((retry_at - datetime.now(timezone.utc)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return None


def get_json(
    url: str,
    headers: dict[str, str] | None,
    params: dict[str, Any] | None,
    timeout: int = 10,
    before_attempt: Callable[[], bool] | None = None,
) -> dict[str, Any] | list[Any] | None:
    """Solicita JSON con errores HTTP explícitos y reintentos solo para 5xx.

    `list` es necesario además de `dict` porque varios endpoints de proveedores
    devuelven arrays JSON en la raíz. Para un 429 se devuelve un objeto con
    `error=rate_limit`; los fallos HTTP restantes y los errores de red devuelven
    `None`.
    """
    _LAST_STATUS_CODE.set(None)
    for attempt in range(3):
        if before_attempt is not None and not before_attempt():
            logger.warning("budget_exhausted_before_attempt url=%s", url)
            return None
        try:
            response = httpx.get(
                url, headers=headers or {}, params=params or {}, timeout=timeout
            )
        except httpx.HTTPError as exc:
            logger.error("http_request_failed url=%s reason=%s", url, exc)
            return None

        _LAST_STATUS_CODE.set(response.status_code)
        if response.status_code == 429:
            return {
                "error": "rate_limit",
                "retry_after_seconds": _retry_after_seconds(
                    response.headers.get("Retry-After")
                ),
            }
        if 500 <= response.status_code <= 599:
            if attempt < 2:
                time.sleep(2**attempt)
                continue
            logger.error(
                "http_server_error url=%s status_code=%s after_retries=2",
                url,
                response.status_code,
            )
            return None
        if 400 <= response.status_code <= 499:
            logger.error(
                "http_client_error url=%s status_code=%s", url, response.status_code
            )
            return None
        if not 200 <= response.status_code <= 299:
            logger.error("http_unexpected_status url=%s status_code=%s", url, response.status_code)
            return None

        try:
            payload = response.json()
        except ValueError:
            logger.error("http_invalid_json url=%s status_code=%s", url, response.status_code)
            return None
        if not isinstance(payload, (dict, list)):
            logger.warning("http_json_unexpected_shape url=%s status_code=%s", url, response.status_code)
            return None
        return payload
    return None

