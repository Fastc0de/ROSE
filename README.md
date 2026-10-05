# ROS

Sistema de investigación profunda y monitorización de fuentes, para la terminal.
Requisitos del producto: [`docs/specs.txt`](docs/specs.txt). Diseño de referencia: [`docs/2026-09-15-ros-complete-application-design.md`](docs/2026-09-15-ros-complete-application-design.md). Plan por fases: [`docs/ROADMAP.md`](docs/ROADMAP.md).

## Estado: en construcción (Fase 0 completada)

Ya hecho:

- `ros/security.py`: validación de URLs y protección SSRF en cada redirección (con la conexión fijada a la IP validada), límite de bytes, lista de tipos MIME permitidos, URLs canónicas y aislamiento del contenido externo frente a prompt injection.
- `ros/db.py`: SQLite con migraciones versionadas y comprobación de checksum, modo WAL, búsqueda FTS5 sobre documentos y afirmaciones, y backups en línea.
- `ros/budget.py`: límites reales de rondas, fuentes, tiempo, coste y tokens. El gasto se reserva antes de cada llamada y el tiempo se conserva entre reinicios.
- `ros/llm.py`: salidas JSON con esquema vía Claude, medición de uso y coste, errores tipados y un `FakeLLM` para tests.
- `ros/connectors/`: RSS/Atom, YouTube (feed oficial del canal), Reddit (RSS público), páginas web con detección de cambios y búsqueda (DuckDuckGo, Brave, SearXNG).
- `ros/research/`: investigación adaptativa por rondas (plan → búsqueda → descarga → extracción → análisis → nuevo plan), con checkpoints para reanudar e informe en Markdown con evidencia trazable.
- `ros/cli.py`: comando `ros research`.
- `ros/offline.py`: modo sin modelo (`--offline` o `ROS_LLM=fake`) para probar ROS sin coste.
- `tests/`: 75 tests sin red ni API keys, ejecutados en CI.

Pendiente:

- Monitorización: seguimientos, cursores, correlación de eventos, digestos y alertas.
- Scheduler/daemon en bucle.
- Resto de la CLI: `runs`, `show`, `resume`, `report`, `search`, `backup` (Fase 1).

Plan completo en [`docs/ROADMAP.md`](docs/ROADMAP.md).

## Requisitos

Python 3.11+ y [uv](https://docs.astral.sh/uv/). Para el análisis hace falta `ANTHROPIC_API_KEY`
(y `ANTHROPIC_WORKSPACE_ID` si tu clave no está asociada a un workspace).

### Modelos por rol

| Rol | Modelo por defecto | Esfuerzo | Etapas |
|---|---|---|---|
| Orquestador | `claude-opus-5-5` | `medium` | plan, análisis de cada ronda, informe final |
| Validador | `claude-sonnet-5-5` | `medium` | revisa que cada conclusión del informe esté respaldada por su evidencia |
| Worker | `claude-haiku-4-5` | — | extracción de afirmaciones de cada documento |

Se cambian en `ros.toml` (`orchestrator_model`, `validator_effort`, `worker_model`…) o con variables
`ROS_ORCHESTRATOR_MODEL`, `ROS_VALIDATOR_EFFORT`, etc.

```bash
uv sync
uv run pytest                                   # tests, sin red
uv run ros research "baterías de sodio" --offline   # prueba sin modelo ni coste
uv run ros research "baterías de sodio" --max-cost 0.5
```
