# Logging y auditoría

## Niveles

`setup_logging` fija el logger raíz y ambos handlers en `INFO`. Solo con `LOG_LEVEL=DEBUG` pasan a `DEBUG`. `INFO` registra eventos normales; `DEBUG` añade diagnóstico; `WARNING`, `ERROR` y `CRITICAL` señalan incidencias. `audit()` usa `ERROR` si la inserción falla. El fichero `app.log` lleva un JSON completo por línea; la consola usa texto.

## request_id

`set_request_id` guarda el valor en un `ContextVar`; `get_request_id` devuelve el activo o `no-request`. `RequestIdFilter` lo inyecta en cada registro. Para llevarlo a un hilo nuevo se usa `contextvars.copy_context().run`; no se pasa como argumento.

## audit_log

Se guardan `actor`, `action`, `entity_id`, `payload_hash`, `outcome` y `created_at_utc` en UTC. El `payload` se serializa a JSON canónico, se hashea con SHA-256 y solo se persiste el hash. El payload original y sus valores no van en `audit_log`: el llamador los guarda en su propia tabla si los necesita.
