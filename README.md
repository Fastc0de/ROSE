# ROS

Sistema de investigación profunda y monitorización de fuentes, para la terminal.
**Para continuar el trabajo: [`docs/HANDOFF.md`](docs/HANDOFF.md).**

Requisitos del producto: [`docs/specs.txt`](docs/specs.txt). Diseño de referencia: [`docs/2026-09-15-ros-complete-application-design.md`](docs/2026-09-15-ros-complete-application-design.md). Plan por fases: [`docs/ROADMAP.md`](docs/ROADMAP.md).

## Estado: fases 0–8 del roadmap implementadas

| Área | Qué hace |
|---|---|
| Investigación (`ros research`) | Plan revisable antes de gastar, rondas adaptativas (buscar → descargar → extraer → analizar → replanificar), subtemas descubiertos, informe Markdown con cada afirmación enlazada a su fuente, validación del informe con un segundo modelo, reanudación, cancelación con informe parcial y reutilización de investigaciones anteriores como contexto (nunca como evidencia). |
| Seguimientos (`ros watch`) | Configuración validada y versionada; ingesta en dos transacciones (los items y el cursor primero, el análisis después), detección de nuevo/modificado por id + hash, línea base en la primera sincronización, duplicados exactos y casi duplicados entre fuentes tratados como corroboración, *circuit breaker* por fuente. |
| Correlación, digestos y alertas | Agrupación de eventos de distintas fuentes (heurística barata + confirmación del modelo), alertas solo por reglas explícitas (importancia, palabras clave, N fuentes independientes) con tope diario, digestos idempotentes con periodo, resumen, grupos, cronología, contradicciones, novedades frente al anterior, confianza y cobertura. Bandeja local (`ros inbox`). |
| Daemon (`ros daemon`) | Un único proceso por base de datos (bloqueo en SQLite): ejecuta seguimientos vencidos, genera digestos, recupera análisis interrumpidos, reanuda investigaciones en cola y entrega notificaciones. Horarios con zona IANA y cambio de hora; apagado limpio con SIGTERM/Ctrl+C. |
| Lenguaje natural (`ros draft`, `--from-text`) | El modelo propone, el código valida y señala lo que requiere tu decisión (credenciales, fuentes ausentes, coste, interpretaciones distintas). Nada recurrente se activa sin confirmación. |
| Conectores | Web, RSS/Atom, YouTube y Reddit por feed público, búsqueda web, y APIs oficiales: YouTube Data API v3 (con cuota), Reddit OAuth, X API v2, Instagram (business discovery) y Facebook (Páginas). Sin credenciales aparecen como `unconfigured` y no se usan; nunca hay scraping de redes sociales. |
| Memoria y evidencia | Búsqueda FTS en documentos, afirmaciones, hallazgos y eventos; reputación de fuentes por dimensiones; feedback explícito que ajusta prioridades de forma visible y reversible; poda de texto antiguo auditada. |
| Entrega e interfaces | Webhook (firmado, con `Idempotency-Key`), Telegram y email; API local con token y panel de solo lectura (`ros serve`); exportación a Obsidian que respeta tus notas. |
| Seguridad | SSRF por salto con conexión fijada a la IP validada, límites de bytes y MIME, contenido externo aislado frente a prompt injection, secretos solo por nombre de variable de entorno, API ligada a 127.0.0.1 con validación de `Host`. |

Los tests (sin red ni API keys) cubren los 14 criterios de aceptación de `specs.txt`; ver la tabla al final de [`docs/ROADMAP.md`](docs/ROADMAP.md).

## Instalación

Python 3.11+ y [uv](https://docs.astral.sh/uv/). Para el análisis hace falta la clave del proveedor de modelos.
Por defecto ROS usa [OpenCode Go](https://opencode.ai/docs/go/): `glm-5.3` para orquestar y validar, y
`deepseek-v4.1-flash` para la extracción y el triaje.

```bash
export OPENCODE_API_KEY="oc_sk_…"     # nunca en ros.toml ni en el repositorio
uv sync
uv run pytest                          # tests, sin red
uv run ros --help
```

DeepSeek en OpenCode Go exige activar **Global** en los ajustes de privacidad (Privacy) de tu workspace de OpenCode;
sin eso, ROS se detiene con ese mensaje.

Cualquier orden acepta `--offline` (o `ROS_LLM=fake`): usa un modelo determinista sin coste para probar el flujo completo.

## Uso

### Investigar

```bash
ros research "baterías de sodio: estado real y competidores" --focus "costes y fabricantes chinos" --max-cost 1
ros runs                         # historial
ros show 3                       # plan, rondas, fuentes, afirmaciones, consumo
ros report 3 -o informe.md
ros errors 3                     # qué falló y qué impacto tuvo
ros resume 3                     # tras Ctrl+C o un corte
ros cancel 3                     # para y deja un informe parcial
ros research "…" --queue         # lo ejecuta el daemon
ros search "densidad energética" # todo lo recopilado
```

Antes de ejecutar se muestra la interpretación, los supuestos, las sub-preguntas, las primeras búsquedas y el
presupuesto, y se pide confirmación (`--yes` en scripts).

### Seguir fuentes

```bash
ros watch add baterias \
  --source rss:https://www.electrive.com/feed/ \
  --source reddit:r/batteries \
  --source youtube:@CATL \
  --source "search:sodium-ion battery" \
  --interest "lanzamientos, precios y fábricas de baterías de sodio" \
  --every 4h --digest daily@20:00 --tz Europe/Madrid \
  --alert-sources 2 --alert-keyword "recall"

ros watch run baterias           # probar ahora
ros watch enable baterias        # que lo ejecute el daemon
ros events baterias              # eventos detectados
ros digest baterias --now        # informe acumulado con lo pendiente
ros inbox                        # alertas, informes y avisos
ros watch edit baterias --every 2h   # nueva versión, con diff
ros watch from-research 3 --name sodio   # convertir una investigación en seguimiento
```

O en lenguaje natural:

```bash
ros draft "Sigue r/batteries y https://www.electrive.com/feed/ cada cuatro horas, \
avísame inmediatamente si varias fuentes independientes reportan el mismo problema \
y entrégame un solo informe al final del día"
```

### Ejecutar en segundo plano

```bash
ros daemon                       # bucle; Ctrl+C o SIGTERM para parar limpiamente
ros daemon --once                # un ciclo (para cron)
```

Hay una unidad de ejemplo para systemd en [`docs/ros-daemon.service`](docs/ros-daemon.service).

### Otras órdenes

```bash
ros connectors [--probe]         # estado real de cada conector
ros sources                      # reputación por fuente
ros feedback event 42 less       # "menos de esto" (también: more, known, useful, wrong)
ros prune --older-than 180       # retención: quita texto completo antiguo, conserva evidencia y metadatos
ros backup                       # copia consistente y verificada
ros serve                        # API local + panel (imprime un enlace de acceso de un solo uso)
ros obsidian ~/Vault             # exportar informes y digestos
```

## Configuración

`ros.toml` en el directorio actual o `~/.config/ros/config.toml`; ejemplo comentado en
[`docs/ros.example.toml`](docs/ros.example.toml). Precedencia: flags > variables `ROS_*` > `--config` > `./ros.toml` >
configuración global > valores por defecto. Los secretos nunca se escriben en el archivo: solo el nombre de la
variable de entorno que los contiene.

### Proveedor y modelos por rol

| `llm` | Clave | Modelos por defecto (orquestador / validador / worker) |
|---|---|---|
| `opencode` (por defecto) | `OPENCODE_API_KEY` | `glm-5.3` / `glm-5.3` / `deepseek-v4.1-flash` |
| `openrouter` | `OPENROUTER_API_KEY` | `openrouter_model` (`openrouter/free`) para los tres |
| `anthropic` | `ANTHROPIC_API_KEY` (+ `ANTHROPIC_WORKSPACE_ID` si hace falta) | `claude-opus-5-5` / `claude-sonnet-5-5` / `claude-haiku-4-5` |
| `fake` | — | modelo determinista sin coste (`--offline`) |

Roles: el **orquestador** planifica, analiza cada ronda, redacta informes, correlaciona y genera digestos; el
**validador** revisa que cada conclusión esté respaldada; el **worker** extrae afirmaciones y hace el triaje de
cada item de los seguimientos. Para cambiar el modelo de un rol: `worker_model = "…"` en `ros.toml` o
`ROS_WORKER_MODEL=…` (igual con `ORCHESTRATOR` y `VALIDATOR`).

Con OpenCode, ROS se identifica con su propio User-Agent y envía una sesión estable (`x-opencode-session`) por
ejecución, como pide OpenCode. El coste se calcula con los precios publicados de OpenCode Go (DeepSeek a tarifa de
hora punta, para no quedarse corto). Con OpenRouter se factura el coste que informa OpenRouter.

En ambos casos ROS pide salida JSON con esquema; si el modelo no la admite, envía el esquema en el prompt. Una
respuesta a la que le falten campos obligatorios se reintenta una vez y, si sigue incompleta, es un error: nunca
se rellena con valores inventados.

### Conectores con credenciales

| Conector | Variables | Notas |
|---|---|---|
| `youtube_api` | `YOUTUBE_API_KEY` | Cuota diaria contabilizada (`youtube_daily_quota`). |
| `reddit_api` | `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` | App OAuth de solo lectura. |
| `x` | `X_BEARER_TOKEN` | Requiere un nivel de acceso de pago a la API v2. |
| `instagram` | `META_ACCESS_TOKEN` + `instagram_user_id` | Solo cuentas profesionales, vía business discovery. |
| `facebook` | `META_ACCESS_TOKEN` | Solo Páginas con permisos concedidos. |

Sin credenciales, `youtube`, `reddit`, `rss`, `web` y `search` siguen funcionando con acceso público.
