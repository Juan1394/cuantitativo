"""Configuración de logging de la aplicación.

No usa la configuración automática de logging: ``setup_logging`` sustituye los
handlers del logger raíz para evitar duplicados. El handler de fichero emite una
línea JSON por registro; el handler de consola emite texto.
"""

import contextvars
import json
import logging
import os
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_REQUEST_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="no-request"
)


def set_request_id(request_id: str) -> None:
    """Fija el identificador de petición en el contexto actual."""

    _REQUEST_ID.set(request_id)


def get_request_id() -> str:
    """Devuelve el identificador activo o ``no-request`` si no hay ninguno."""

    return _REQUEST_ID.get()


class RequestIdFilter(logging.Filter):
    """Inyecta ``request_id`` y recoge los campos extra en cada registro."""

    _RESERVED = frozenset(
        logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
    ) | {"asctime", "message", "request_id"}

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id()
        extras: dict[str, Any] = {}
        for key, value in record.__dict__.items():
            if key in self._RESERVED or key.startswith("_"):
                continue
            extras[key] = value
        record._json_extra = extras
        return True


class JsonFormatter(logging.Formatter):
    """Serializa cada registro como un único objeto JSON en una línea."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "no-request"),
            "extra": getattr(record, "_json_extra", {}),
        }
        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        )


def setup_logging(settings: Any) -> None:
    """Configura el logger raíz con un handler de fichero y otro de stdout."""

    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    app_level = (
        logging.DEBUG
        if os.environ.get("LOG_LEVEL") == "DEBUG"
        else logging.INFO
    )

    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        handler.close()
    root.setLevel(app_level)

    file_handler = RotatingFileHandler(
        log_dir / "app.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=7,
        encoding="utf-8",
    )
    file_handler.setLevel(app_level)
    file_handler.setFormatter(JsonFormatter())
    file_handler.addFilter(RequestIdFilter())

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(app_level)
    stream_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    )
    stream_handler.addFilter(RequestIdFilter())

    root.addHandler(file_handler)
    root.addHandler(stream_handler)


def get_logger(name: str) -> logging.Logger:
    """Devuelve un logger hijo del logger raíz."""

    return logging.getLogger(name)
