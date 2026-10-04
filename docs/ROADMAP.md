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

## Estado actual (lo que ya está en `main`)

| Módulo | Qué hace | Estado |
|---|---|---|
| `ros/security.py` | SSRF por salto, límite de bytes, MIME permitidos, URL canónica, aislamiento anti prompt injection | Hecho; conexión fijada a la IP validada |
| `ros/db.py` | SQLite + WAL, migraciones con checksum, FTS5, backup en línea, locks | Hecho; el esquema ya incluye tablas de seguimientos, eventos y digestos |
| `ros/budget.py` | Reserva antes de gastar, tiempo que sobrevive a reinicios | Hecho |
| `ros/llm.py` | Salidas JSON con esquema, coste medido, errores tipados, `FakeLLM` | Hecho y verificado contra el SDK 1.x |
| `ros/connectors/` | RSS/Atom, YouTube (feed), Reddit (RSS), web con detección de cambios, buscadores | Hecho, con tests |
| `ros/research/` | Investigación por rondas con checkpoints e informe Markdown trazable | Hecho, con tests de extremo a extremo |
| `ros/cli.py` | Script `ros` | `ros research`; el resto llega en la Fase 1 |
| `ros/offline.py` | Modo `llm="fake"` / `--offline` | Hecho |
| `ros/monitor/` | Seguimientos, eventos, digestos | Vacío |
| `tests/` | Tests sin red, en CI (GitHub Actions) | Hecho |

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

## Fase 1 — Investigación profunda usable desde la terminal

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

---

## Fase 2 — Seguimientos (monitorización)

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

---

## Fase 3 — Correlación, digestos y alertas

**Objetivo:** dejar de notificar publicaciones sueltas y entregar informes agrupados.

- Agrupar eventos en `clusters`: primero con heurística barata (entidades y términos compartidos, ventana temporal),
  luego el modelo confirma, titula y explica la relación.
- Digest Markdown con periodo, resumen, acontecimientos, grupos, cronología, contradicciones, confianza,
  novedades frente al digest anterior, preguntas abiertas, qué se sigue vigilando y cobertura/fuentes caídas.
- Alertas inmediatas solo cuando se cumplen reglas explícitas (umbral de importancia, varias fuentes independientes).
- Bandeja local: `ros inbox`, `ros digest <seguimiento> [--now]`. Notificaciones idempotentes (`dedupe_key`).

**Salida:** 9 relación entre fuentes · 10 informe acumulado. Repetir la generación no duplica digestos.

---

## Fase 4 — Scheduler y daemon

**Objetivo:** que los seguimientos corran solos y resistan reinicios.

- `ros daemon`: un bucle que toma un lock en SQLite, ejecuta seguimientos vencidos (`next_run_at`), genera digestos
  (`next_digest_at`) y reanuda investigaciones interrumpidas.
- Horarios en UTC, con zona horaria IANA del usuario ("cada 4 horas", "a las 20:00").
- Reintentos con backoff, `Retry-After` y un circuit breaker por fuente. Apagado limpio con SIGTERM/Ctrl+C.
- Reloj inyectable para testear horarios y cambios de horario de verano.
- Opcional: unidad `systemd --user` o `launchd` de ejemplo.

**Salida:** un seguimiento programado se ejecuta, se cae a mitad, y al reiniciar el daemon termina sin duplicar nada.

---

## Fase 5 — Configuración en lenguaje natural

**Objetivo:** escribir "Revisa estas fuentes cada 4 horas y dame un informe al final del día" y obtener una configuración revisable.

- `ConfigurationDraft`: el modelo propone un objeto estructurado y el código lo valida. Se muestra un diff y se pide confirmación.
  Nunca se activa nada recurrente sin confirmación explícita.
- `ros watch add --from-text "..."` y `ros research "..."` usan el mismo mecanismo.
- "Convertir investigación en seguimiento": crea un borrador de `WatchSpec` a partir de las fuentes y consultas de una investigación.
- Pide intervención cuando falten credenciales, el coste supere el presupuesto o haya interpretaciones muy distintas.

**Salida:** las cuatro frases de ejemplo de specs producen configuraciones correctas y editables (tests con `FakeLLM`).

---

## Fase 6 — Conectores nativos (APIs oficiales)

- Contrato ampliado: `capabilities()`, `status` (`unconfigured`, `ready`, `degraded`, `auth_expired`, `quota_blocked`…)
  y una sonda real antes de anunciar el conector como disponible.
- **YouTube Data API v3**: búsqueda, metadatos y comentarios, con reserva de cuota.
- **Reddit OAuth**: posts y comentarios completos, paginación por `fullname`.
- X, Instagram y Facebook solo si consigues acceso oficial; mientras tanto se muestran como `unconfigured` y nunca se sustituyen por scraping.
- Tests de contrato con fixtures, más tests en vivo opcionales con credenciales.

---

## Fase 7 — Memoria de conocimiento y calidad de la evidencia

- Reutilizar hallazgos de investigaciones anteriores como contexto ("lo que ya se sabe") sin tratarlos como evidencia nueva.
- Reputación de fuente por dimensiones (autoridad, independencia, fuente primaria, conflictos de interés).
- Dedupe por similitud (no solo hash exacto), conservando todos los miembros y su procedencia.
- Retención por niveles (texto completo frente a metadatos) y `ros prune` auditable.
- Feedback explícito ("más de esto", "ya lo sabía") que cambia la prioridad sin borrar historial.

---

## Fase 8 — Interfaces y entrega

- Notificaciones externas: email, webhook o Telegram, con entrega idempotente.
- API local (FastAPI en `127.0.0.1`, con token) y un dashboard sencillo de solo lectura.
- Exportación unidireccional a Obsidian.

---

## Fuera de alcance por ahora

LEARN, LocalPulse/Discover avanzado, TikTok, embeddings y despliegue multi-nodo. Están en el diseño de referencia
como trabajo futuro y se pueden retomar cuando las fases 0–5 estén sólidas.

## Mapa de criterios de aceptación (specs.txt)

| # | Criterio | Fase |
|---:|---|---|
| 1 | Investigación de varias rondas | 0–1 |
| 2 | Modificar el plan según hallazgos | 1 |
| 3 | Respetar todos los límites | 0–1 |
| 4 | Mostrar fuentes y evidencia | 1 |
| 5 | Reanudar una tarea interrumpida | 1 (investigación), 4 (seguimientos) |
| 6 | Seguimiento sobre varias fuentes | 2 |
| 7 | Detectar solo contenido nuevo | 2 |
| 8 | Evitar duplicados | 2 |
| 9 | Relacionar eventos entre fuentes | 3 |
| 10 | Informe acumulado | 3 |
| 11 | Hechos, inferencias y rumores | 1 |
| 12 | Explicar errores y parciales | 0–1 |
| 13 | Conservar historial | 1–2 |
| 14 | Contenido externo no controla al agente | 0 (y en cada fase) |
