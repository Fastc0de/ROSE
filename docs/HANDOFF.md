# ROS — Traspaso para continuar en otra sesión

Fecha: 2026-10-05. Rama de trabajo fusionada en `main`. Este documento es el punto de partida del próximo chat.
Léelo junto con [`specs.txt`](specs.txt) (visión), el [diseño de referencia](2026-09-15-ros-complete-application-design.md)
y el [roadmap](ROADMAP.md).

## 1. Visión (recordatorio)

ROS es un sistema personal de inteligencia, descubrimiento e investigación, para no depender de revisar a mano redes
sociales y páginas. No es un buscador que devuelve resultados: debe **construir y actualizar un estado de
conocimiento** sobre lo que sigue.

```
Web/RSS · YouTube · Reddit · X · Instagram · Facebook
  → Adapters → NormalizedItem → snapshots/dedupe → claims + evidence
  → KnowledgeState versionado → ChangeEvents → clusters → digest/alertas → Obsidian
```

Cuatro workflows: **RESEARCH** (investigar y contrastar), **MONITOR** (vigilar y agrupar cambios), **LEARN**
(convertir conocimiento en material para aprender), **DISCOVER** (descubrimiento amplio).
Dos tipos de seguimiento: **AmbientAwareness** ("¿qué pasa de interesante en mis áreas?") y **ExplicitWatch**
("vigila esta cuenta/página/canal"). **LocalPulse / Maracaibo**: eventos, aperturas/cierres, comida, cultura,
nightlife, noticias locales y tendencias, agrupados.
Jerarquía de fuentes: nivel 1 Web, RSS, YouTube, Reddit · nivel 2 X (API oficial de pago, si aporta) ·
nivel 3 Instagram/Facebook (cuentas profesionales y páginas, vía API oficial).
Principio: **no descartar información de fuentes secundarias**; guardarla con menos peso o confianza.
Decisión de diseño vigente: sin scraping autenticado de redes sociales.

## 2. Qué existe hoy (en `main`)

Todo en Python 3.11+, `uv`, SQLite. 150 tests sin red (`uv run pytest`), CI en GitHub Actions (3.11 y 3.12).

| Área | Módulos | Estado |
|---|---|---|
| Seguridad | `ros/security.py` | SSRF por salto con IP fijada, límites de bytes/MIME, aislamiento anti prompt injection |
| Persistencia | `ros/db.py` | Migraciones v1 y v2, FTS5 (items, claims, findings, events), backup, locks con detección de dueño muerto |
| Presupuestos | `ros/budget.py` | Reserva antes de gastar; tiempo persistente |
| Modelos | `ros/llm.py`, `ros/registry.py`, `ros/config.py` | Proveedores: `opencode` (por defecto), `openrouter`, `anthropic`, `fake` |
| RESEARCH | `ros/research/` | Rondas adaptativas, plan revisable, reanudar/cancelar, informe trazable, validación |
| MONITOR | `ros/monitor/` | WatchSpec versionado, ingesta A/B, nuevo/modificado, dedupe y casi-duplicados, circuit breaker, correlación en clusters, alertas por reglas, digestos idempotentes, daemon, configuración en lenguaje natural |
| Conectores | `ros/connectors/` | web, rss, youtube y reddit (feed), search; APIs oficiales: youtube_api, reddit_api, x, instagram, facebook (sin credenciales = `unconfigured`) |
| Conocimiento | `ros/knowledge.py` | Conocimiento previo como contexto, reputación por dimensiones, feedback, poda, búsqueda en corpus |
| Entrega e interfaces | `ros/notify.py`, `ros/api.py`, `ros/obsidian.py` | Webhook/Telegram/email, API local + panel, exportación a Obsidian |
| CLI | `ros/cli.py` | `research, runs, show, resume, cancel, report, errors, search, backup, watch …, events, digest, inbox, draft, daemon, connectors, sources, feedback, prune, serve, obsidian, deliver` |

### Configuración de modelos actual

- `llm = "opencode"` usa OpenCode Go (`https://opencode.ai/zen/go/v1`, compatible con OpenAI).
  - orquestador y validador: `glm-5.3`; worker (extracción y triaje): `deepseek-v4.1-flash`.
  - OpenCode Go exige `User-Agent` propio y `x-opencode-session` estable (ROS usa uno por ejecución).
  - DeepSeek exige **Global** en Privacy del workspace de OpenCode (ya activado por el usuario).
  - DeepSeek usa `response_format: json_object` + esquema en el prompt (con `json_schema` razona hasta agotar
    tokens). Fallback genérico: si un modelo agota tokens sin responder bajo `json_schema`, se reintenta en `json_object`.
  - Respuestas con campos obligatorios ausentes se reintentan una vez y luego son error (nunca se inventan).
- `llm = "openrouter"`: modelo por defecto `openrouter/free`.
- Claves solo por variable de entorno: `OPENCODE_API_KEY`, `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`.
  **Nunca** en `ros.toml` ni en el repositorio.

## 3. Lo aprendido en las pruebas reales (5 oct 2026)

Investigación real "baterías de iones de sodio: estado comercial en 2026":

1. **Con modelos gratuitos de OpenRouter**: completó (14 min, $0), pero la validación agotó tokens, las
   afirmaciones salieron en inglés y un documento se perdió por JSON inválido.
2. **Con OpenCode (GLM-5.3 + DeepSeek)**: buscar + leer 5 páginas + extraer costó ~4 min y ~$0.04. Después,
   **GLM-5.3 pasó 13 min razonando en el análisis de la ronda** y agotó 2×16k tokens sin contestar
   (~$0.16 perdidos). Se detuvo a mano.

Diagnóstico: **el sistema de búsqueda y análisis está mal planteado** y no escala:

- El buscador es DuckDuckGo leído como HTML: corta conexiones, ~6 resultados, sin fechas.
- Se descargan candidatos solo por el título: entran granjas SEO y webs bloqueadas (3/8 con 403).
- Se envía cada página entera (hasta 12k caracteres) y se extraen 8–32 afirmaciones por página, muchas en inglés.
- **El análisis de ronda es un embudo**: manda *todas* las afirmaciones acumuladas al orquestador y le pide todo en
  una sola respuesta (hallazgos, contradicciones, vacíos, sub-preguntas, subtemas, consultas, cobertura). Crece cada
  ronda y los modelos con razonamiento se atascan.
- Todo es secuencial, sin tope de tiempo por llamada y sin progreso visible.

Fallos pendientes detectados:

- Si la validación falla, el informe queda como "completa" (debería ser "parcial").
- "✅ verificado" se asigna a todas las afirmaciones de un hallazgo con ≥2 dominios, aunque cada afirmación tenga
  una sola fuente. Debe exigir corroboración de la propia afirmación.
- `pkill -f "ros research"` en scripts mata también el shell que lo invoca (usar el PID).

## 4. Lo acordado para la siguiente sesión (por este orden)

### Paso 1 — Rehacer la capa de búsqueda y lectura (aprobado por el usuario)

1. **Buscador por API como predeterminado**: Brave Search API, Tavily y SearXNG propio (Docker); DuckDuckGo solo
   como respaldo. Varias búsquedas en paralelo, resultados fusionados (p. ej. Reciprocal Rank Fusion), deduplicados
   por URL canónica y con fechas. Si un motor falla, siguen los demás. **No** raspar las páginas de resultados de
   Google o Brave (va contra sus condiciones y se bloquea).
2. **Filtrar antes de descargar**: puntuación barata por relevancia del texto de muestra frente a las sub-preguntas,
   reputación del dominio (`knowledge.source_reputation`) y heurísticas de granja de contenido. Se descargan los
   mejores; **el resto se conserva en el corpus con su puntuación**, no se descarta.
3. **Leer solo lo relevante**: trocear cada página en fragmentos, quedarse con los que responden a las sub-preguntas
   (BM25 o similar) y enviar solo esos. **Máximo ~8 afirmaciones por documento, en el idioma del usuario.**
   Mejorar la extracción de texto (la actual es básica, solo stdlib).
4. **Analizar por partes**: el worker resume cada sub-pregunta por separado (estado, evidencia, contradicciones). El
   orquestador solo recibe un **resumen compacto** y decide qué buscar después. Cada ronda debe costar lo mismo que
   la primera.
5. **Velocidad y control**: lecturas y extracciones en paralelo; **tope de tiempo por llamada al modelo** (p. ej.
   3 min; si se pasa, se registra y se sigue); pedir razonamiento bajo o nulo en las etapas pesadas si el proveedor lo
   permite (comprobar con una llamada mínima); **progreso visible** en pantalla.
6. **Rastreador propio** (la parte de "scraping" que sí tiene sentido): seguir enlaces dentro de temas/sitios,
   autodescubrir RSS y sitemaps, respetar robots.txt y limitar la velocidad por dominio, y usar navegador real
   (Playwright, opcional) para webs que solo cargan con JavaScript. Base para DISCOVER.
7. Corregir los fallos pendientes de la sección 3.

Decisiones que el usuario aún no ha contestado (preguntar al empezar):

- ¿Docker disponible para SearXNG? (si no, funcionar sin él)
- ¿Clave de Brave Search API y/o Tavily? (planes gratuitos)
- ¿Instalar Playwright (~150 MB)?

### Paso 2 — KnowledgeState versionado y ChangeEvents reales

Hoy un "evento" es "salió una publicación relevante". Debe ser "cambió lo que sabemos sobre X": estado de
conocimiento por tema/entidad, versionado y append-only, con cada versión trazable a sus afirmaciones y evidencias;
los ChangeEvents salen de comparar versiones. Ver secciones 5 y 8 del diseño.

### Paso 3 — DISCOVER + AmbientAwareness + LocalPulse (Maracaibo)

- DISCOVER y AmbientAwareness: exploración acotada por intereses, novedad y presupuesto de atención; propuestas de
  nuevas fuentes (`PromotionProposal`) que el usuario acepta o rechaza; nunca crear seguimientos sin confirmación.
- LocalPulse Maracaibo: eventos, lugares, aperturas/cierres, comida, cultura, nightlife, noticias, tendencias.
  Fuentes viables: medios locales (RSS/web), r/maracaibo, webs de eventos, búsqueda web y **cuentas profesionales de
  Instagram** vía la API oficial (conector `instagram` ya implementado; requiere `META_ACCESS_TOKEN` +
  `instagram_user_id`). TikTok y perfiles personales quedan fuera. Agrupar por "Para hacer / Para saber / Para
  conversar" (sección 9 del diseño).

### Paso 4 — LEARN

Convertir el conocimiento en material para aprender: rutas de aprendizaje sobre el grafo de temas, separar lo que ROS
sabe de lo que el usuario domina, sesiones reanudables (sección 7 del diseño).

## 5. Cómo arrancar el próximo chat

1. Abrir una sesión nueva sobre el repositorio `Fastc0de/ROSE` (rama `main`).
2. Pegar el texto de la sección 6 como primer mensaje.
3. Si la sesión es en la nube: permitir en el entorno los dominios `opencode.ai` (y `openrouter.ai`,
   `api.search.brave.com`, `api.tavily.com` si se van a usar) en *Network access*.
4. Proporcionar las claves como variables de entorno del entorno o de la terminal, no en el chat si se puede evitar.
   **Regenerar las claves de OpenRouter y OpenCode que se pegaron en el chat anterior.**

## 6. Mensaje inicial para el próximo chat

```
Estoy construyendo ROS (repo Fastc0de/ROSE, rama main). Lee primero docs/HANDOFF.md, luego docs/specs.txt,
docs/ROADMAP.md y el diseño en docs/2026-09-15-ros-complete-application-design.md.

Quiero continuar con el "Paso 1 — Rehacer la capa de búsqueda y lectura" de HANDOFF.md, completo
(buscador por API multi-motor con fechas, filtrado antes de descargar sin descartar nada, fragmentos relevantes
con máx. ~8 afirmaciones por documento en mi idioma, análisis por sub-pregunta con resumen compacto para el
orquestador, paralelismo + tope de tiempo por llamada + progreso visible, rastreador propio con robots.txt y
Playwright opcional, y los fallos pendientes de la sección 3).

Después sigue con los pasos 2, 3 y 4 en ese orden, con tests sin red para todo, commits por paso y push a tu rama.

Modelos: OpenCode Go (llm = "opencode"): glm-5.3 para orquestar y validar, deepseek-v4.1-flash para el resto.
La clave está en OPENCODE_API_KEY. Pregúntame al empezar lo de Docker/SearXNG, la clave de Brave/Tavily y Playwright.
Explícame las cosas de forma sencilla: no soy muy técnico.
```
