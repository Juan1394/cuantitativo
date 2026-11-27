import contextvars
import json
import logging
import sys
import threading
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import logging_config as lc


@pytest.fixture
def restore_logging():
    root = logging.getLogger()
    old_handlers = root.handlers[:]
    old_level = root.level
    lc._REQUEST_ID.set("no-request")
    yield
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        handler.close()
    for handler in old_handlers:
        root.addHandler(handler)
    root.setLevel(old_level)
    lc._REQUEST_ID.set("no-request")


def _logging_settings(tmp_path, log_level="INFO"):
    return SimpleNamespace(log_dir=tmp_path / "logs", log_level=log_level)


def _settings_class(monkeypatch):
    monkeypatch.setenv("API_FOOTBALL_KEY", "test-football")
    monkeypatch.setenv("FOOTBALL_DATA_ORG_KEY", "test-data-org")
    monkeypatch.setenv("SHARP_ODDS_API_KEY", "test-sharp")
    from config import Settings

    return Settings


def _leer_request_id() -> str:
    return lc.get_request_id()


def _fijar_y_leer(request_id: str) -> str:
    lc.set_request_id(request_id)
    return _leer_request_id()


def _ancestors(logger: logging.Logger):
    current = logger
    while current is not None:
        yield current
        current = current.parent


def test_log_info_llega_a_fichero_json(tmp_path, restore_logging):
    settings = _logging_settings(tmp_path)
    lc.setup_logging(settings)

    logger = lc.get_logger("test.logging")
    logger.info("mensaje de prueba", extra={"campo": "valor"})

    log_file = settings.log_dir / "app.log"
    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1

    payload = json.loads(lines[0])
    assert set(payload) == {
        "timestamp",
        "level",
        "logger",
        "message",
        "request_id",
        "extra",
    }
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test.logging"
    assert payload["message"] == "mensaje de prueba"
    assert payload["request_id"] == "no-request"
    assert payload["extra"] == {"campo": "valor"}

    timestamp = datetime.fromisoformat(payload["timestamp"])
    assert timestamp.tzinfo is not None
    assert timestamp.utcoffset() == timedelta(0)


def test_extra_vacio_cuando_no_hay_campos_personalizados(tmp_path, restore_logging):
    settings = _logging_settings(tmp_path)
    lc.setup_logging(settings)

    lc.get_logger("test.sin_extra").info("sin extras")

    lines = (settings.log_dir / "app.log").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["extra"] == {}


def test_request_id_se_propaga_entre_funciones(restore_logging):
    assert _fijar_y_leer("req-123") == "req-123"
    assert lc.get_request_id() == "req-123"


def test_request_id_viaja_a_hilo_con_copy_context(restore_logging):
    lc.set_request_id("req-hilo")
    contexto = contextvars.copy_context()
    resultado: list[str] = []

    def tarea() -> None:
        resultado.append(lc.get_request_id())

    hilo = threading.Thread(target=contexto.run, args=(tarea,))
    hilo.start()
    hilo.join()

    assert resultado == ["req-hilo"]


def test_request_id_aparece_en_json(tmp_path, restore_logging):
    settings = _logging_settings(tmp_path)
    lc.setup_logging(settings)
    lc.set_request_id("req-json")

    lc.get_logger("test.request").info("con identificador")

    lines = (settings.log_dir / "app.log").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["request_id"] == "req-json"


def test_log_level_debug(monkeypatch, tmp_path, restore_logging):
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    lc.setup_logging(_logging_settings(tmp_path, "DEBUG"))

    root = logging.getLogger()
    assert root.level == logging.DEBUG
    assert all(handler.level == logging.DEBUG for handler in root.handlers)

    lc.get_logger("test.debug").debug("traza")
    lines = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["level"] == "DEBUG"


def test_log_level_info_por_defecto(monkeypatch, tmp_path, restore_logging):
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    lc.setup_logging(_logging_settings(tmp_path))

    root = logging.getLogger()
    assert root.level == logging.INFO
    assert all(handler.level == logging.INFO for handler in root.handlers)

    logger = lc.get_logger("test.info")
    logger.debug("no debe escribirse")
    logger.info("si debe escribirse")

    lines = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["level"] == "INFO"


def test_setup_logging_no_duplica_handlers(tmp_path, restore_logging):
    settings = _logging_settings(tmp_path)
    lc.setup_logging(settings)
    lc.setup_logging(settings)

    root = logging.getLogger()
    assert len(root.handlers) == 2
    assert sum(isinstance(h, RotatingFileHandler) for h in root.handlers) == 1
    assert any(
        isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.FileHandler)
        and h.stream is sys.stdout
        for h in root.handlers
    )


def test_rotating_file_handler_parametros(tmp_path, restore_logging):
    settings = _logging_settings(tmp_path)
    lc.setup_logging(settings)

    handler = next(
        h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler)
    )
    assert handler.maxBytes == 10 * 1024 * 1024
    assert handler.backupCount == 7
    assert handler.encoding == "utf-8"
    assert Path(handler.baseFilename) == (settings.log_dir / "app.log").resolve()


def test_stream_handler_formato_texto(tmp_path, restore_logging):
    lc.setup_logging(_logging_settings(tmp_path))

    handler = next(
        h
        for h in logging.getLogger().handlers
        if isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.FileHandler)
    )
    assert handler.formatter._fmt == (
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )


def test_get_logger_es_descendiente_del_raiz():
    logger = lc.get_logger("app.modulo")
    assert logger is not logging.getLogger()
    assert logger.name == "app.modulo"
    assert logging.getLogger() in list(_ancestors(logger))


def test_log_level_se_carga_desde_env_y_configura_logging(
    monkeypatch, tmp_path, restore_logging
):
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    Settings = _settings_class(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "API_FOOTBALL_KEY=test-football\n"
        "FOOTBALL_DATA_ORG_KEY=test-data-org\n"
        "SHARP_ODDS_API_KEY=test-sharp\n"
        f"LOG_DIR={tmp_path / 'logs'}\n"
        "LOG_LEVEL=DEBUG\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)
    assert settings.log_level == "DEBUG"
    lc.setup_logging(settings)
    assert logging.getLogger().level == logging.DEBUG


def test_log_level_invalido_lanza_validation_error(monkeypatch, tmp_path):
    from pydantic import ValidationError

    monkeypatch.delenv("LOG_LEVEL", raising=False)
    Settings = _settings_class(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "API_FOOTBALL_KEY=test-football\n"
        "FOOTBALL_DATA_ORG_KEY=test-data-org\n"
        "SHARP_ODDS_API_KEY=test-sharp\n"
        "LOG_LEVEL=INVALID\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        Settings(_env_file=env_file)


def test_json_formatter_incluye_traceback_solo_con_excepcion(
    tmp_path, restore_logging
):
    settings = _logging_settings(tmp_path)
    lc.setup_logging(settings)
    logger = lc.get_logger("test.exception")

    try:
        raise ValueError("causa original")
    except ValueError:
        logger.error("fallo", exc_info=True)
    logger.info("sin excepción")

    lines = (settings.log_dir / "app.log").read_text(encoding="utf-8").splitlines()
    with_exception, without_exception = [json.loads(line) for line in lines]
    assert "exception" in with_exception
    assert "causa original" in with_exception["exception"]
    assert "exception" not in without_exception
