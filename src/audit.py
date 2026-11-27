"""Inserción de registros de auditoría en ``audit_log``."""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from src.logging_config import get_logger

logger = get_logger(__name__)


def audit(
    conn: sqlite3.Connection,
    actor: str,
    action: str,
    entity_id: str | None,
    payload: dict | None,
    outcome: str,
) -> None:
    """Inserta una fila de auditoría con el hash del payload, no el payload."""

    try:
        canonical_payload = json.dumps(
            payload if payload is not None else {},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        payload_hash = hashlib.sha256(
            canonical_payload.encode("utf-8")
        ).hexdigest()
        created_at_utc = datetime.now(timezone.utc).isoformat()
    except Exception:
        logger.error("audit insert failed", exc_info=True)
        return None

    try:
        conn.execute("SAVEPOINT audit_ins")
        conn.execute(
            "INSERT INTO audit_log "
            "(actor, action, entity_id, payload_hash, outcome, created_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                actor,
                action,
                entity_id,
                payload_hash,
                outcome,
                created_at_utc,
            ),
        )
        conn.execute("RELEASE SAVEPOINT audit_ins")
    except Exception:
        try:
            conn.execute("ROLLBACK TO SAVEPOINT audit_ins")
            conn.execute("RELEASE SAVEPOINT audit_ins")
        except Exception:
            pass
        logger.error("audit insert failed", exc_info=True)
        return None

    return None
