# ROS

Sistema de investigación profunda y monitorización de fuentes, para la terminal.
Requisitos del producto: [`docs/specs.txt`](docs/specs.txt). Diseño de referencia: [`docs/2026-09-15-ros-complete-application-design.md`](docs/2026-09-15-ros-complete-application-design.md).

## Estado: en construcción (aún no se puede usar)

Ya hecho:

- `ros/security.py`: validación de URLs y protección SSRF en cada redirección, límite de bytes, lista de tipos MIME permitidos, URLs canónicas y aislamiento del contenido externo frente a prompt injection.
- `ros/db.py`: SQLite con migraciones versionadas y comprobación de checksum, modo WAL, búsqueda FTS5 sobre documentos y afirmaciones, y backups en línea.
- `ros/budget.py`: límites reales de rondas, fuentes, tiempo, coste y tokens. El gasto se reserva antes de cada llamada y el tiempo se conserva entre reinicios.
- `ros/llm.py`: salidas JSON con esquema vía Claude, medición de uso y coste, errores tipados y un `FakeLLM` para tests.
- `ros/connectors/`: RSS/Atom, YouTube (feed oficial del canal), Reddit (RSS público), páginas web con detección de cambios y búsqueda (DuckDuckGo, Brave, SearXNG).
- `ros/research/`: investigación adaptativa por rondas (plan → búsqueda → descarga → extracción → análisis → nuevo plan), con checkpoints para reanudar e informe en Markdown con evidencia trazable.

Pendiente:

- Monitorización: seguimientos, cursores, correlación de eventos, digestos y alertas.
- Scheduler/daemon en bucle.
- CLI `ros` (`ros/cli.py`). El script `ros` declarado en `pyproject.toml` todavía no funciona.
- Modo offline (`ros/offline.py`), referenciado desde `ros/registry.py`.
- Tests automatizados.

## Requisitos

Python 3.11+ y [uv](https://docs.astral.sh/uv/). Para el análisis hace falta `ANTHROPIC_API_KEY`.

```bash
uv sync
```
