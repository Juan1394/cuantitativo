# BACKLOG

Items conocidos que no bloquean Fase 1 y se abordan post-Fase 3, cuando haya datos reales que proteger.

## F05 — JSON malformado en `config.py` produce traceback

- **Severidad:** BAJA (cosmético)
- **Repro:** `ACTIVE_COMPETITIONS="["` en `.env` → `config.py` devuelve exit 1 pero con traceback de `JSONDecodeError` en lugar de mensaje limpio.
- **Test:** `tests/test_F05.py`
- **Fase recomendada:** post-Fase 3

## F09 — `EV_THRESHOLD=inf` aceptado

- **Severidad:** MEDIA (validación)
- **Repro:** `EV_THRESHOLD=inf` en `.env` → `config.py` devuelve exit 0.
- **Test:** `tests/test_F09.py`
- **Fase recomendada:** post-Fase 3

## F10 — `repr(settings)` filtra claves

- **Severidad:** MEDIA (seguridad)
- **Repro:** `repr(settings)` incluye los valores de `api_football_key`, `football_data_org_key`, `sharp_odds_api_key` y `sharp_odds_fallback_api_key` en texto plano.
- **Test:** `tests/test_F10.py`
- **Fase recomendada:** post-Fase 3 (mitigado parcialmente con `SecretStr` en ronda 4)

## F15 — Dos sentencias SQL en una línea

- **Severidad:** BAJA (defensivo)
- **Repro:** migración con `CREATE TABLE a(x); CREATE TABLE b(x);` en una línea → falla con `ProgrammingError`.
- **Test:** `tests/test_F15.py`
- **Fase recomendada:** post-Fase 3

## F17 — Saltos de versión intermedios

- **Severidad:** BAJA (defensivo)
- **Repro:** desde V4, con 0005 ausente y 0006 presente → aplica 0006 sin aviso.
- **Test:** `tests/test_F17.py`
- **Fase recomendada:** post-Fase 3
