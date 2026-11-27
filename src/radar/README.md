# Ingestión de proveedores

Este bloque consulta fixtures y cuotas externas; las respuestas se mantienen en su forma JSON de origen. No realiza normalización de equipos, snapshots ni validaciones de calidad. Las fechas persistidas deben mantenerse en UTC; la conversión a `Europe/Madrid` corresponde a la interfaz.

## Proveedores, límites y endpoints

- **The Odds API**: 500 peticiones mensuales en el plan indicado. Solo `get_odds` consume el presupuesto (`the_odds_api/month`); `list_sports`, `list_events` y `odds_api_quota` son gratuitos según el plan descrito. `list_sports` se cachea en memoria durante 24 horas. Se conservan Pinnacle (`pinnacle`) y Betfair Exchange (`betfair_ex_eu`).
- **football-data.org v4**: 10 peticiones por minuto. `get_matches` y `get_match` cuentan una petición (`football_data_org/minute`). Se consulta por competición, nunca el listado global.
- **API-Football v3**: 100 peticiones diarias. `get_fixtures` consume (`api_football/day`).

Los contadores se guardan en `rate_limit_budget`; aplicar `migrations/0006_rate_limit_budget.sql` antes de instanciar el gestor. Cada consumo es atómico con `BEGIN IMMEDIATE`.

## Ligas admitidas

| Código interno | The Odds API `sport_key` | football-data.org | API-Football `league_id` |
|---|---|---|---:|
| SP1 | `soccer_spain_la_liga` | `PD` | 140 |
| E0 | `soccer_epl` | `PL` | 39 |
| I1 | `soccer_italy_serie_a` | `SA` | 135 |

## Añadir un proveedor

Añade su par proveedor/ventana y límite en `PROVIDER_LIMITS`, crea una migración si requiere persistencia adicional, y consume con `RateLimitBudgetManager.consume` antes de la petición. El gestor serializa escrituras por transacción SQLite y no depende de código específico de endpoints; no modifiques sus contadores directamente desde clientes.

## Snapshot recorder

`SnapshotRecorder` agrupa los planes por liga y realiza una sola llamada de The Odds API por competición, no una llamada por evento. Esta agrupación es crítica para respetar el presupuesto mensual de 500 créditos. La ejecución está protegida por `snapshot_execution_enabled=False` por defecto; solo se habilita explícitamente cuando el proveedor sharp es The Odds API.

Los resultados usan `valid` cuando se escriben cuotas sharp, `gap` para fallos o eventos ausentes, y `skipped` cuando no corresponde guardar cuotas. Los `skip_reasons` admitidos son `daily_cap_reached`, `too_far_from_kickoff`, `budget_exhausted`, `no_odds_available`, `league_not_in_response`, `api_error` y `dry_run_disabled`.

La política que decide qué eventos deben planificarse (límites diarios, ventanas y prioridades de negocio) pertenece a Fase 3, no al recorder. El plan solo filtra estado, kickoff y competiciones activas, y evita capturas demasiado recientes.
