# ROS — Plan de implementación por fases

Este plan ordena la construcción de ROS a partir del código que ya existe en `main`.
Toma como meta los 14 criterios de aceptación de [`specs.txt`](specs.txt) y usa el
[documento de diseño](2026-09-15-ros-complete-application-design.md) como referencia,
**no como algo que haya que implementar entero**. Ese diseño está pensado para un equipo
(API, dashboard, LEARN, LocalPulse, Obsidian, 17 workstreams). Aquí se construye primero un
núcleo fiable en la terminal y se añade lo demás encima.

Reglas para todas las fases:

- Cada fase se entrega en su propio PR y no se pasa a la siguiente sin cumplir su criterio de salida.
- Tests con fakes por defecto: `FakeLLM`, `StaticSearchBackend` y `httpx.MockTransport`. Nada de red ni de API keys.
  Los tests contra servicios reales son opcionales y van marcados aparte.
- La seguridad no se deja para el final: todo conector nuevo usa `SafeFetcher`, `wrap_untrusted` y el `Ledger`.

---

## Estado actual

Las fases 0–8 están implementadas. Cada fase indica abajo qué se construyó y dónde está.
Lo que queda fuera de alcance sigue en la última sección.

| Módulo | Qué hace |
|---|---|
| `ros/security.py` | SSRF por salto con conexión fijada a la IP validada, límite de bytes, MIME permitidos, URL canónica, aislamiento anti prompt injection |
| `ros/db.py` | SQLite + WAL, migraciones con checksum (v1 base, v2 seguimientos/entrega/conocimiento), FTS5 en documentos, afirmaciones, hallazgos y eventos, backup en línea, locks |
| `ros/budget.py` | Reserva antes de gastar, tiempo que sobrevive a reinicios |
| `ros/llm.py` | Salidas JSON con esquema, coste medido, errores tipados, `FakeLLM` |
| `ros/research/` | Investigación por rondas con checkpoints, plan revisable, cancelación, reanudación, informe trazable |
| `ros/monitor/` | `spec` (WatchSpec), `service` (ingesta A/B), `correlate`, `digest` (alertas y digestos), `schedule`, `daemon`, `drafts` (lenguaje natural) |
| `ros/connectors/` | Web, RSS/Atom, YouTube y Reddit por feed, búsqueda; `native.py`: YouTube Data API, Reddit OAuth, X, Instagram, Facebook |
| `ros/knowledge.py` | Casi duplicados, conocimiento previo, reputación, feedback, poda, búsqueda en el corpus |
| `ros/notify.py`, `ros/api.py`, `ros/obsidian.py` | Entrega externa, API local + panel, exportación a Obsidian |
| `ros/cli.py` | Todas las órdenes `ros …` |
| `tests/` | 136 tests sin red ni API keys, en CI (Python 3.11 y 3.12) |

---

## Fase 0 — Estabilizar la base ✅

**Objetivo:** que lo que ya existe esté probado y no tenga bugs conocidos antes de construir encima.

Tareas:

1. Infraestructura de tests: `tests/conftest.py` con BD en memoria, `FakeLLM`, buscador estático y
   fetcher con `httpx.MockTransport` y un resolver DNS falso.
2. Tests unitarios de `security` (IPs privadas, redirecciones a localhost, credenciales en la URL,
   dominios parecidos, MIME, límite de bytes), `db` (migración, checksum, FTS, backup),
   `budget` (reservas, tiempo entre reinicios), `connectors` (RSS, Atom, YouTube y Reddit con fixtures) y `config` (precedencia).
3. Test de extremo a extremo del `ResearchEngine` con fakes: varias rondas, plan que cambia, reanudación tras interrumpir.
4. Bugs encontrados al revisar el código:
   - **`BudgetExhausted` se traga durante la extracción.** En `engine._extract`, `except RosError` captura el agotamiento
     del presupuesto (no es `fatal`). Cada documento restante queda marcado como `failed`, cuando debería
     quedar pendiente y detener la ronda. Eso infla el recuento de fallos y puede marcar el informe como degradado por el motivo equivocado.
   - **DNS rebinding.** `validate_url` resuelve el host y luego httpx lo vuelve a resolver al conectar. Hay que conectar a la IP ya validada
     (transport propio o `httpx` con resolución fijada) para cerrar la ventana TOCTOU.
   - **`ros/offline.py` no existe.** Implementarlo como un handler determinista para que `ROS_LLM=fake` funcione en demos.
   - Verificar la llamada a Claude en `llm.py` (parámetros de `output_config`, betas, tabla de precios) contra la documentación actual.
5. CLI mínima (`ros/cli.py`, con `argparse` o Typer): `ros research "<objetivo>"` y `ros --version`.

**Salida:** `uv run pytest` en verde, sin red. `ROS_LLM=fake ros research "x"` genera un informe Markdown.

---

## Fase 1 — Investigación profunda usable desde la terminal ✅

**Objetivo:** que puedas usar ROS a diario para investigar.

- Comandos: `ros research` (con `--focus`, `--exclude`, `--max-cost`, `--max-rounds`…), `ros runs`, `ros show <id>`,
  `ros resume <id>`, `ros report <id> [-o archivo.md]`, `ros errors <id>`, `ros search "<texto>"` (FTS sobre documentos y
  afirmaciones) y `ros backup`.
- Ctrl+C pausa limpiamente y `ros resume` continúa desde el último checkpoint.
- Revisión del plan antes de ejecutar: muestra la interpretación, los supuestos y el presupuesto, y pide confirmación (`--yes` en modo batch).
- Política de parada: no marcar como "parcial" una investigación que solo tocó el tope de fuentes tras cubrir el plan.
- Reutilizar conocimiento previo: si una fuente ya está en la BD, se reutiliza en vez de descargarla de nuevo (ya funciona; añadir el test).

**Salida (criterios de specs):** 1 varias rondas · 2 el plan cambia según hallazgos · 3 límites respetados ·
4 fuentes y evidencia · 5 reanudación · 11 hechos/inferencias/rumores · 12 errores y parciales · 13 historial.

**Hecho:** todas las órdenes anteriores más `ros cancel` (detiene en un punto seguro y deja informe parcial) y
`ros research --queue` (lo ejecuta el daemon). Un bloqueo por investigación impide ejecutarla en dos procesos.
El plan también extrae del texto el foco, los dominios a excluir y si hay interpretaciones alternativas.

---

## Fase 2 — Seguimientos (monitorización) ✅

**Objetivo:** crear seguimientos sobre varias fuentes y detectar solo lo nuevo.

- `WatchSpec` validado (Pydantic): fuentes, interés en lenguaje natural, exclusiones, frecuencia, umbral de alerta,
  horario del digest y duración. Se guarda versionado (`watch_versions`).
- Comandos: `ros watch add|list|show|edit|enable|disable|run <nombre>`. La edición crea una versión nueva.
- Ingesta en dos pasos, como dice el diseño:
  **A)** se guardan los items, snapshots y cursor, y el análisis queda marcado como pendiente, todo en una transacción.
  **B)** se analizan los pendientes fuera de la transacción.
  Si el proceso muere entre A y B, el análisis se reanuda sin volver a descargar.
- Detección de nuevo/modificado mediante `external_id` + hash de contenido + ETag/Last-Modified.
- Análisis de relevancia por item con el modelo: relevancia, importancia, tipo de afirmación, etiquetas y por qué importa → `events`.
- Dedupe entre fuentes por URL canónica y hash.

**Salida:** 6 seguimiento sobre varias fuentes · 7 solo contenido nuevo · 8 sin duplicados.
Ejecutar dos veces seguidas no genera eventos duplicados.

**Hecho:** además, `ros watch history|delete|from-research`, línea base en la primera sincronización (`backfill`),
cursores por seguimiento y fuente (dos seguimientos sobre el mismo feed ven sus items), copias exactas o casi
idénticas de algo que el seguimiento ya tiene enlazadas como fuente adicional del evento (corroboración),
profundidad `deep` que descarga la página completa cuando el feed trae solo un resumen, y diff contra la versión
anterior cuando un item cambia. Las reglas de exclusión del usuario se aplican antes de gastar en el modelo.

---

## Fase 3 — Correlación, digestos y alertas ✅

**Objetivo:** dejar de notificar publicaciones sueltas y entregar informes agrupados.

- Agrupar eventos en `clusters`: primero con heurística barata (entidades y términos compartidos, ventana temporal),
  luego el modelo confirma, titula y explica la relación.
- Digest Markdown con periodo, resumen, acontecimientos, grupos, cronología, contradicciones, confianza,
  novedades frente al digest anterior, preguntas abiertas, qué se sigue vigilando y cobertura/fuentes caídas.
- Alertas inmediatas solo cuando se cumplen reglas explícitas (umbral de importancia, varias fuentes independientes).
- Bandeja local: `ros inbox`, `ros digest <seguimiento> [--now]`. Notificaciones idempotentes (`dedupe_key`).

**Salida:** 9 relación entre fuentes · 10 informe acumulado. Repetir la generación no duplica digestos.

**Hecho:** `ros events <seguimiento>`, tope diario de alertas (las que sobran van al digest), digestos que se
generan aunque falle el modelo (marcados como limitados) y sección de cobertura con las fuentes que fallaron.

---

## Fase 4 — Scheduler y daemon ✅

**Objetivo:** que los seguimientos corran solos y resistan reinicios.

- `ros daemon`: un bucle que toma un lock en SQLite, ejecuta seguimientos vencidos (`next_run_at`), genera digestos
  (`next_digest_at`) y reanuda investigaciones interrumpidas.
- Horarios en UTC, con zona horaria IANA del usuario ("cada 4 horas", "a las 20:00").
- Reintentos con backoff, `Retry-After` y un circuit breaker por fuente. Apagado limpio con SIGTERM/Ctrl+C.
- Reloj inyectable para testear horarios y cambios de horario de verano.
- Opcional: unidad `systemd --user` o `launchd` de ejemplo.

**Salida:** un seguimiento programado se ejecuta, se cae a mitad, y al reiniciar el daemon termina sin duplicar nada.

**Hecho:** `ros daemon [--once]`, expiración de seguimientos con duración, tope global de gasto diario
(`monitor_daily_cost_usd`), recuperación de análisis pendientes limitada para no entrar en bucle y unidad de
ejemplo en `docs/ros-daemon.service`. La ventana de recuperación y el circuit breaker están en
`ros/monitor/daemon.py` y `ros/monitor/service.py`.

---

## Fase 5 — Configuración en lenguaje natural ✅

**Objetivo:** escribir "Revisa estas fuentes cada 4 horas y dame un informe al final del día" y obtener una configuración revisable.

- `ConfigurationDraft`: el modelo propone un objeto estructurado y el código lo valida. Se muestra un diff y se pide confirmación.
  Nunca se activa nada recurrente sin confirmación explícita.
- `ros watch add --from-text "..."` y `ros research "..."` usan el mismo mecanismo.
- "Convertir investigación en seguimiento": crea un borrador de `WatchSpec` a partir de las fuentes y consultas de una investigación.
- Pide intervención cuando falten credenciales, el coste supere el presupuesto o haya interpretaciones muy distintas.

**Salida:** las cuatro frases de ejemplo de specs producen configuraciones correctas y editables (tests con `FakeLLM`).

**Hecho:** `ros draft "…"` detecta si es investigación o seguimiento; `ros watch add --from-text` y
`ros watch edit --from-text` (con diff). Los borradores quedan guardados en `config_drafts`. En modo offline un
intérprete por reglas sustituye al modelo, así que las frases de ejemplo funcionan sin API key.

---

## Fase 6 — Conectores nativos (APIs oficiales) ✅

- Contrato ampliado: `capabilities()`, `status` (`unconfigured`, `ready`, `degraded`, `auth_expired`, `quota_blocked`…)
  y una sonda real antes de anunciar el conector como disponible.
- **YouTube Data API v3**: búsqueda, metadatos y comentarios, con reserva de cuota.
- **Reddit OAuth**: posts y comentarios completos, paginación por `fullname`.
- X, Instagram y Facebook solo si consigues acceso oficial; mientras tanto se muestran como `unconfigured` y nunca se sustituyen por scraping.
- Tests de contrato con fixtures, más tests en vivo opcionales con credenciales.

**Hecho:** `ros connectors [--probe]`. Además de YouTube y Reddit, X (API v2), Instagram (business discovery) y
Facebook (Páginas) existen como conectores de API oficial, pero solo se activan con credenciales y una sonda
correcta. **Están probados contra fixtures con la forma documentada de cada API, no contra los servicios reales:**
conviene una prueba en vivo con tus credenciales antes de confiar en ellos.

---

## Fase 7 — Memoria de conocimiento y calidad de la evidencia ✅

- Reutilizar hallazgos de investigaciones anteriores como contexto ("lo que ya se sabe") sin tratarlos como evidencia nueva.
- Reputación de fuente por dimensiones (autoridad, independencia, fuente primaria, conflictos de interés).
- Dedupe por similitud (no solo hash exacto), conservando todos los miembros y su procedencia.
- Retención por niveles (texto completo frente a metadatos) y `ros prune` auditable.
- Feedback explícito ("más de esto", "ya lo sabía") que cambia la prioridad sin borrar historial.

**Hecho:** `ros sources`, `ros feedback`, `ros prune`, `ros search` (también sobre hallazgos y eventos). La
reputación se calcula a partir de lo almacenado (se puede reconstruir) y desempata la selección de fuentes
después de la diversidad. El feedback multiplica la importancia de eventos parecidos (±15 % por voto, entre 0,5 y
1,5) y el motivo queda guardado en el evento.

---

## Fase 8 — Interfaces y entrega ✅

- Notificaciones externas: email, webhook o Telegram, con entrega idempotente.
- API local (FastAPI en `127.0.0.1`, con token) y un dashboard sencillo de solo lectura.
- Exportación unidireccional a Obsidian.

**Hecho:** `ros deliver` y entrega automática desde el daemon (webhook firmado con HMAC e `Idempotency-Key`,
Telegram, email). `ros serve`: API en `/api/v1` con token en `~/.ros/api_token` (permisos 600), panel de solo lectura
con enlace de acceso de un solo uso, validación de `Host` y mutaciones solo con token. `ros obsidian`: bloques
gestionados con checksum, conflicto si los editas a mano, escritura atómica y rutas confinadas al vault.

---

## Fuera de alcance por ahora

LEARN, LocalPulse/Discover avanzado, TikTok, embeddings y despliegue multi-nodo. Están en el diseño de referencia
como trabajo futuro y se pueden retomar cuando las fases 0–5 estén sólidas.

## Mapa de criterios de aceptación (specs.txt)

| # | Criterio | Fase | Tests |
|---:|---|---|---|
| 1 | Investigación de varias rondas | 0–1 | `test_research_engine::test_adaptive_multi_round_research` |
| 2 | Modificar el plan según hallazgos | 1 | ídem (subtema descubierto, versión 2 del plan) |
| 3 | Respetar todos los límites | 0–1 | `test_budget_exhaustion_…`, `test_source_cap_…`, `test_db_budget_config` |
| 4 | Mostrar fuentes y evidencia | 1 | informe con `## Evidencia` y fuentes; `test_cli::test_research_lifecycle` |
| 5 | Reanudar una tarea interrumpida | 1, 4 | `test_interrupted_run_resumes_…`, `test_crash_between_ingest_and_analysis_…`, `test_shutdown_request_requeues_the_run` |
| 6 | Seguimiento sobre varias fuentes | 2 | `test_monitor::test_only_new_content_and_no_duplicates` |
| 7 | Detectar solo contenido nuevo | 2 | ídem (segunda ejecución sin eventos; item editado → `modified`) |
| 8 | Evitar duplicados | 2 | `test_cross_source_duplicate_…`, `test_near_duplicates_…`, digestos idempotentes |
| 9 | Relacionar eventos entre fuentes | 3 | `test_correlation_alerts_and_idempotent_digest` |
| 10 | Informe acumulado | 3 | ídem y `test_daemon_schedules_watches_and_digests` |
| 11 | Hechos, inferencias y rumores | 1 | tipos de afirmación en informes y digestos |
| 12 | Explicar errores y parciales | 0–1 | `ros errors`, sección de errores/cobertura, `test_failing_source_opens_circuit_…` |
| 13 | Conservar historial | 1–2 | versiones de plan y de seguimiento, ejecuciones conservadas al borrar un seguimiento |
| 14 | Contenido externo no controla al agente | 0 (y en cada fase) | `test_security`, detección de inyección en extracción y triaje |
