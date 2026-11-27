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

## Radar · rate_limit.py sin cubrir edge cases de frontera de ventana

- **Severidad:** BAJA (precisión de cuota)
- **Cobertura actual:** 81% (L43-44, L57, L63, L95-99, L106, L121-124, L138-141, L163-166)
- **Riesgo:** consume() en el instante exacto de la frontera puede desviar 1 crédito.
- **Fase recomendada:** post-Fase 3

## Radar · the_odds_api.py sin cubrir parseo de bookmakers complejos

- **Severidad:** BAJA
- **Cobertura actual:** 85% (L62, L69, L74-78, L95, L102-107, L141, L143-148, L154-159)
- **Riesgo:** bookmaker sin campo markets rompe el parseo.
- **Fase recomendada:** post-Fase 3

## Radar · api_football.py sin cubrir paths de error

- **Severidad:** BAJA
- **Cobertura actual:** 88% (L42, L46-47)
- **Riesgo:** error 429/5xx de API-Football no loggea con contexto.
- **Fase recomendada:** post-Fase 3

## Normalization · test_abbreviation insuficiente

- **Severidad:** BAJA (cobertura, no funcional)
- **Motivo:** test_normalize_team_name_abbreviation no demuestra Utd→United. El seed no tiene Newcastle. El test admite (None, None) y un pending.
- **Fase recomendada:** post-Fase 3 (ampliar seed en migración futura)

## Normalization · test_ambiguous no verifica warning

- **Severidad:** BAJA
- **Motivo:** test_normalize_team_name_ambiguous_returns_none no usa caplog para verificar "ambiguous_fuzzy_match".
- **Fase recomendada:** post-Fase 3

## Normalization · contrato de retorno con alias pending no documentado

- **Severidad:** MEDIA (riesgo de integridad en Fase 1.3.2)
- **Motivo:** normalize_team_name devuelve canonical aunque status='pending'. El recorder podría escribir snapshots con canonical no confirmado.
- **Fase recomendada:** Fase 1.3.2 (al integrar en snapshot_recorder)

## Normalization · cobertura al 85%

- **Severidad:** BAJA
- **Cobertura actual:** 85% (20 statements sin cubrir en paths de error)
- **Fase recomendada:** post-Fase 3

## Normalization · test_abbreviation insuficiente

- **Severidad:** BAJA
- **Motivo:** test_normalize_team_name_abbreviation no demuestra Utd→United. El seed no tiene Newcastle.
- **Fase recomendada:** post-Fase 3 (ampliar seed en migración futura)

## Normalization · test_ambiguous no verifica warning

- **Severidad:** BAJA
- **Motivo:** test_normalize_team_name_ambiguous_returns_none no usa caplog para verificar "ambiguous_fuzzy_match".
- **Fase recomendada:** post-Fase 3

## Normalization · cobertura al 88%

- **Severidad:** BAJA
- **Cobertura actual:** 88% (paths de error no cubiertos)
- **Fase recomendada:** post-Fase 3

## Snapshot recorder · cobertura al 91%

- **Severidad:** BAJA
- **Cobertura actual:** 91% (paths de error no cubiertos)
- **Fase recomendada:** post-Fase 3

## PROCESO · Hardcode de versión en tests de migración

- **Severidad:** MEDIA (deuda de proceso, no técnica)
- **Motivo:** el patrón "assert user_version == N" ha reaparecido 4 veces en 4 rondas con migración nueva (0006, 0007, 0008, 0009). Cada vez se ha corregido, y cada vez vuelve cuando se añade una migración.
- **Acción de proceso:** en cada prompt que añada migración, incluir sección "MIGRACIONES AFECTADAS" con la regla explícita: "Ningún test nuevo debe verificar user_version == N. Derivar de list_migrations(). Si necesitas verificar estado intermedio, aplicar solo hasta esa migración y derivar de la lista filtrada."
- **Fase recomendada:** aplica a todos los prompts futuros con migración (0010+, 0011+, etc.)
