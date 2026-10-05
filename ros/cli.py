"""Command-line entry point: `ros`."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__
from .config import load_settings, ros_home
from .db import loads
from .errors import NeedsUserAction, RosError

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_INTERRUPTED = 0, 1, 2, 130


class UsageError(ValueError):
    pass


def _echo(message: str = "") -> None:
    print(message, flush=True)


def _confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise UsageError(f"{question} — se necesita confirmación: repite la orden con --yes")
    return input(f"{question} [s/N] ").strip().lower() in ("s", "si", "sí", "y", "yes")


def _short(text: str | None, n: int = 70) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


# =========================================================================== parser
def _budget_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--max-rounds", type=int)
    p.add_argument("--max-sources", type=int)
    p.add_argument("--max-minutes", type=float)
    p.add_argument("--max-cost", type=float, help="coste máximo en USD")


def _watch_flags(p: argparse.ArgumentParser, *, edit: bool = False) -> None:
    p.add_argument("--source", action="append", default=[], metavar="TIPO:LOCALIZADOR",
                   help="fuente (repetible): rss:URL, web:URL, youtube:@canal, reddit:r/x, search:texto, "
                        "youtube_api:@canal, reddit_api:r/x, x:@cuenta, instagram:usuario, facebook:página, o una URL")
    p.add_argument("--interest", help="qué te interesa, en lenguaje natural")
    p.add_argument("--topic", action="append", default=[], help="qué detectar (repetible)")
    p.add_argument("--exclude", action="append", default=[], help="qué ignorar (repetible)")
    p.add_argument("--every", help="frecuencia de revisión: 30m, 4h, 1d")
    p.add_argument("--digest", help="informe: daily@20:00, weekly@mon@09:00, every:12h, threshold:5, manual")
    p.add_argument("--alert-importance", type=float, help="alerta inmediata desde esta importancia (0-1)")
    p.add_argument("--alert-sources", type=int, help="alerta si N fuentes independientes informan de lo mismo")
    p.add_argument("--alert-keyword", action="append", default=[], help="alerta si aparece esta expresión")
    p.add_argument("--no-alerts", action="store_true", help="desactivar alertas inmediatas")
    p.add_argument("--depth", choices=["light", "standard", "deep"])
    p.add_argument("--max-cost", type=float, help="coste máximo por ejecución (USD)")
    p.add_argument("--tz", help="zona horaria IANA del horario (p. ej. Europe/Madrid)")
    p.add_argument("--duration", help="duración del seguimiento: 7d, 2w… (por defecto indefinido)")
    p.add_argument("--from-text", help="describir el seguimiento en lenguaje natural")
    p.add_argument("--no-validate", action="store_true", help="no consultar las fuentes para validarlas")
    p.add_argument("--yes", "-y", action="store_true", help="confirmar sin preguntar")
    if edit:
        p.add_argument("--remove-source", action="append", default=[], metavar="TIPO:LOCALIZADOR")
    else:
        p.add_argument("--enable", action="store_true", help="activar al crear (pide confirmación)")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ros", description="Investigación profunda y monitorización de fuentes.")
    p.add_argument("--version", action="version", version=f"ros {__version__}")
    p.add_argument("--config", help="archivo TOML de configuración")
    p.add_argument("--offline", dest="global_offline", action="store_true",
                   help="sin modelo de lenguaje (demo, sin coste ni análisis real)")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("research", help="investigar un tema en varias rondas")
    r.add_argument("objective", help="qué quieres investigar, en lenguaje natural")
    r.add_argument("--focus", default="", help="aspectos a los que prestar especial atención")
    r.add_argument("--exclude", action="append", default=[], metavar="DOMINIO", help="dominio a excluir (repetible)")
    _budget_flags(r)
    r.add_argument("--offline", action="store_true", help="sin modelo de lenguaje (demo, sin coste ni análisis real)")
    r.add_argument("-o", "--output", help="dónde guardar el informe Markdown")
    r.add_argument("--yes", "-y", action="store_true", help="ejecutar sin pedir confirmación del plan")
    r.add_argument("--queue", action="store_true", help="dejarla en cola para `ros daemon` (sin revisar el plan)")

    x = sub.add_parser("runs", help="listar investigaciones y ejecuciones")
    x.add_argument("--kind", choices=["research", "monitor", "digest", "all"], default="research")
    x.add_argument("--limit", type=int, default=20)

    for name, help_ in (("show", "detalle de una investigación"), ("errors", "errores de una ejecución"),
                        ("cancel", "cancelar una investigación (conserva un informe parcial)")):
        x = sub.add_parser(name, help=help_)
        x.add_argument("run_id", type=int)

    x = sub.add_parser("resume", help="reanudar una investigación pausada o interrumpida")
    x.add_argument("run_id", type=int)
    x.add_argument("-o", "--output")

    x = sub.add_parser("report", help="mostrar o guardar el informe de una investigación")
    x.add_argument("run_id", type=int)
    x.add_argument("-o", "--output")

    x = sub.add_parser("search", help="buscar en todo lo recopilado (documentos, afirmaciones, hallazgos, eventos)")
    x.add_argument("text")
    x.add_argument("--limit", type=int, default=10)

    x = sub.add_parser("backup", help="copia de seguridad consistente de la base de datos")
    x.add_argument("dest", nargs="?")

    # -- watches ------------------------------------------------------------
    w = sub.add_parser("watch", help="seguimientos de fuentes y temas")
    ws = w.add_subparsers(dest="watch_command", required=True)
    x = ws.add_parser("add", help="crear un seguimiento")
    x.add_argument("name", nargs="?")
    _watch_flags(x)
    x = ws.add_parser("edit", help="editar un seguimiento (crea una versión nueva)")
    x.add_argument("name")
    _watch_flags(x, edit=True)
    ws.add_parser("list", help="listar seguimientos")
    for name, help_ in (("show", "ver configuración y estado"), ("enable", "activar"), ("disable", "desactivar"),
                        ("run", "ejecutar ahora"), ("history", "versiones y ejecuciones"), ("delete", "eliminar")):
        x = ws.add_parser(name, help=help_)
        x.add_argument("name")
        if name in ("enable", "delete"):
            x.add_argument("--yes", "-y", action="store_true")
    x = ws.add_parser("from-research", help="convertir una investigación en un seguimiento (borrador revisable)")
    x.add_argument("run_id", type=int)
    x.add_argument("--name", required=True)
    x.add_argument("--every", default="1d")
    x.add_argument("--enable", action="store_true")
    x.add_argument("--yes", "-y", action="store_true")

    x = sub.add_parser("events", help="eventos detectados por un seguimiento")
    x.add_argument("name")
    x.add_argument("--limit", type=int, default=30)

    x = sub.add_parser("digest", help="informes acumulados de un seguimiento")
    x.add_argument("name")
    x.add_argument("--now", action="store_true", help="generar ahora con lo pendiente")
    x.add_argument("--list", action="store_true")
    x.add_argument("--show", type=int, metavar="ID")
    x.add_argument("-o", "--output")

    x = sub.add_parser("inbox", help="bandeja local de alertas, informes y avisos")
    x.add_argument("--all", action="store_true", help="incluir leídas")
    x.add_argument("--show", type=int, metavar="ID", help="mostrar una y marcarla como leída")
    x.add_argument("--read-all", action="store_true", help="marcar todas como leídas")

    x = sub.add_parser("draft", help="configurar en lenguaje natural (investigación o seguimiento)")
    x.add_argument("text")
    x.add_argument("--name")
    x.add_argument("--source", action="append", default=[])
    x.add_argument("--enable", action="store_true")
    x.add_argument("--yes", "-y", action="store_true")

    x = sub.add_parser("daemon", help="ejecutar seguimientos, informes e investigaciones en cola")
    x.add_argument("--once", action="store_true", help="un solo ciclo (útil con cron)")
    x.add_argument("--poll", type=float, default=30.0, help="segundos entre ciclos")

    x = sub.add_parser("connectors", help="conectores disponibles y su estado")
    x.add_argument("--probe", action="store_true", help="comprobar en vivo los conectores con credenciales")

    x = sub.add_parser("sources", help="reputación de las fuentes por dimensiones")
    x.add_argument("--limit", type=int, default=30)

    x = sub.add_parser("feedback", help="decir a ROS qué te sirve (cambia prioridades, no borra historial)")
    x.add_argument("target", choices=["event", "cluster", "digest", "finding", "source"])
    x.add_argument("target_id", help="id, o dominio para 'source'")
    x.add_argument("value", choices=["more", "less", "known", "useful", "wrong"])
    x.add_argument("--note", default="")

    x = sub.add_parser("prune", help="retención: borrar texto completo antiguo (conserva metadatos y evidencia)")
    x.add_argument("--older-than", type=int, required=True, metavar="DÍAS")
    x.add_argument("--dry-run", action="store_true")
    x.add_argument("--yes", "-y", action="store_true")

    x = sub.add_parser("serve", help="API local y panel de solo lectura")
    x.add_argument("--host")
    x.add_argument("--port", type=int)

    x = sub.add_parser("obsidian", help="exportar informes y digestos a un vault de Obsidian")
    x.add_argument("vault", nargs="?")
    x.add_argument("--dry-run", action="store_true")

    sub.add_parser("deliver", help="enviar ahora las notificaciones pendientes a los canales configurados")
    return p


# =========================================================================== helpers
def _budget_overrides(args: argparse.Namespace) -> dict:
    pairs = {"max_rounds": getattr(args, "max_rounds", None), "max_sources": getattr(args, "max_sources", None),
             "max_minutes": getattr(args, "max_minutes", None), "max_cost_usd": getattr(args, "max_cost", None)}
    return {k: v for k, v in pairs.items() if v is not None}


def _offline(args) -> bool:
    return bool(getattr(args, "global_offline", False) or (args.command == "research" and args.offline))


def _app(args, build_app=None, *, budget: dict | None = None):
    from .registry import build_app as default_build
    overrides: dict = {"llm": "fake" if _offline(args) else None}
    if budget:
        overrides["budget"] = budget
    settings = load_settings(args.config, overrides)
    return (build_app or default_build)(settings)


def _write_report(app, run_id: int, output: str | None) -> Path | None:
    row = app.db.one("SELECT report_md FROM runs WHERE id=?", (run_id,))
    if not row or not row["report_md"]:
        return None
    path = Path(output) if output else Path(app.settings.reports_dir) / f"research-{run_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(row["report_md"], encoding="utf-8")
    return path


def _run_research(app, engine, run_id: int, output: str | None) -> int:
    try:
        status = engine.run(run_id)
    except KeyboardInterrupt:
        _echo(f"\nPausada. El progreso está guardado: `ros resume {run_id}`.")
        return EXIT_INTERRUPTED
    path = _write_report(app, run_id, output)
    _echo(f"\nEstado: {status}. " + (f"Informe: {path}" if path else "No se generó informe."))
    return EXIT_FAILED if status == "failed" else EXIT_OK


def _render_plan(plan: dict, cfg: dict, budget: dict) -> None:
    _echo(f"\nInterpretación: {plan.get('interpretation', '')}")
    if plan.get("assumptions"):
        _echo("Supuestos: " + "; ".join(plan["assumptions"]))
    if cfg.get("focus"):
        _echo(f"Atención especial a: {cfg['focus']}")
    if cfg.get("exclude_domains"):
        _echo("Dominios excluidos: " + ", ".join(cfg["exclude_domains"]))
    _echo("Sub-preguntas:")
    for sq in plan.get("subquestions", []):
        _echo(f"  - [{sq['id']}] {sq['question']}")
    _echo("Primeras búsquedas: " + "; ".join(q["text"] for q in plan.get("queries", [])))
    if plan.get("stop_criteria"):
        _echo(f"Criterio de parada: {plan['stop_criteria']}")
    _echo(f"Presupuesto: {budget['max_rounds']} rondas · {budget['max_sources']} fuentes · "
          f"{budget['max_minutes']:g} min · ${budget['max_cost_usd']:.2f} · {budget['max_tokens']} tokens")
    if len(plan.get("alternative_interpretations") or []) > 0:
        _echo("⚠ El objetivo admite otras interpretaciones:")
        for alt in plan["alternative_interpretations"]:
            _echo(f"  - {alt}")


# =========================================================================== research
def cmd_research(args, build_app=None) -> int:
    from .research.engine import ResearchEngine
    app = _app(args, build_app, budget=_budget_overrides(args))
    engine = ResearchEngine(app, echo=_echo)
    run_id = engine.create(args.objective, focus=args.focus, exclude_domains=tuple(args.exclude),
                           status="queued" if args.queue else "planned")
    _echo(f"Investigación #{run_id}")
    if args.queue:
        _echo("En cola: la ejecutará `ros daemon`.")
        return EXIT_OK
    try:
        plan = engine.prepare(run_id)
    except KeyboardInterrupt:
        _echo(f"\nPausada antes de planificar: `ros resume {run_id}`.")
        return EXIT_INTERRUPTED
    run = app.db.one("SELECT config_json, budget_json FROM runs WHERE id=?", (run_id,))
    _render_plan(plan, loads(run["config_json"]), loads(run["budget_json"]))
    if not _confirm("\n¿Ejecutar la investigación con este plan?", args.yes):
        engine.cancel(run_id)
        _echo(f"No ejecutada. El plan queda guardado en la investigación #{run_id}.")
        return EXIT_OK
    return _run_research(app, engine, run_id, args.output)


def cmd_resume(args, build_app=None) -> int:
    from .research.engine import ResearchEngine
    app = _app(args, build_app)
    run = app.db.one("SELECT status, kind FROM runs WHERE id=?", (args.run_id,))
    if run is None or run["kind"] != "research":
        raise UsageError(f"no existe la investigación #{args.run_id}")
    if run["status"] in ("completed", "partial", "cancelled"):
        _echo(f"La investigación #{args.run_id} ya terminó ({run['status']}). Informe: `ros report {args.run_id}`.")
        return EXIT_OK
    _echo(f"Reanudando la investigación #{args.run_id} ({run['status']})…")
    return _run_research(app, ResearchEngine(app, echo=_echo), args.run_id, args.output)


def cmd_cancel(args, build_app=None) -> int:
    from .research.engine import ResearchEngine
    app = _app(args, build_app)
    status = ResearchEngine(app, echo=_echo).cancel(args.run_id)
    _echo({"cancelling": "Cancelación solicitada: el proceso que la ejecuta se detendrá en el próximo punto seguro."}
          .get(status, f"Estado: {status}."))
    return EXIT_OK


def cmd_runs(args, build_app=None) -> int:
    app = _app(args, build_app)
    where = "" if args.kind == "all" else "WHERE r.kind=?"
    params = () if args.kind == "all" else (args.kind,)
    rows = app.db.all(f"""SELECT r.id, r.kind, r.status, r.created_at, r.objective, w.name watch,
                           (SELECT COALESCE(SUM(cost_usd),0) FROM usage u WHERE u.run_id=r.id) cost
                           FROM runs r LEFT JOIN watches w ON w.id=r.watch_id {where} ORDER BY r.id DESC LIMIT ?""",
                      (*params, args.limit))
    if not rows:
        _echo("No hay ejecuciones todavía.")
        return EXIT_OK
    _echo(f"{'#':>5}  {'tipo':8} {'estado':10} {'fecha':16} {'coste':>8}  objetivo")
    for r in rows:
        label = f"[{r['watch']}] " if r["watch"] else ""
        _echo(f"{r['id']:>5}  {r['kind']:8} {r['status']:10} {r['created_at'][:16].replace('T', ' '):16} "
              f"${r['cost']:>7.3f}  {label}{_short(r['objective'], 60)}")
    return EXIT_OK


def cmd_show(args, build_app=None) -> int:
    app = _app(args, build_app)
    db = app.db
    r = db.one("SELECT * FROM runs WHERE id=?", (args.run_id,))
    if r is None:
        raise UsageError(f"no existe la ejecución #{args.run_id}")
    _echo(f"#{r['id']} · {r['kind']} · {r['status']} (etapa: {r['stage']})")
    _echo(f"Objetivo: {r['objective']}")
    _echo(f"Creada: {r['created_at']} · terminada: {r['finished_at'] or '—'}")
    if r["outcome_note"]:
        _echo(f"Resultado: {r['outcome_note']}")
    plan = loads(r["plan_json"], {}) or {}
    if plan:
        _echo(f"Plan v{r['plan_version']}: {len(plan.get('subquestions', []))} sub-preguntas")
        for sq in plan.get("subquestions", []):
            origin = " (descubierta)" if sq.get("origin") == "discovery" else ""
            _echo(f"  - [{sq['id']}] {sq['question']} — {sq.get('status', 'open')}{origin}")
    for rnd in db.all("SELECT * FROM rounds WHERE run_id=? ORDER BY n", (args.run_id,)):
        an = loads(rnd["analysis_json"], {}) or {}
        cov = f", cobertura {an['coverage']:.0%}" if "coverage" in an else ""
        q = db.one("SELECT COUNT(*) c FROM queries WHERE run_id=? AND round_n=?", (args.run_id, rnd["n"]))["c"]
        _echo(f"Ronda {rnd['n']}: {rnd['stage']}{cov}, {q} consultas")
    src = db.all("SELECT status, COUNT(*) c FROM run_items WHERE run_id=? GROUP BY status", (args.run_id,))
    if src:
        _echo("Fuentes: " + ", ".join(f"{s['status']} {s['c']}" for s in src))
    claims = db.all("SELECT claim_type, COUNT(*) c FROM claims WHERE run_id=? GROUP BY claim_type", (args.run_id,))
    if claims:
        _echo("Afirmaciones: " + ", ".join(f"{c['claim_type']} {c['c']}" for c in claims))
    u = db.one("""SELECT COALESCE(SUM(cost_usd),0) c, COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o,
                  SUM(kind='llm') calls FROM usage WHERE run_id=?""", (args.run_id,))
    _echo(f"Consumo: ${u['c']:.4f} · tokens {u['i']}+{u['o']} · llamadas {u['calls'] or 0} · "
          f"tiempo {r['elapsed_s'] / 60:.1f} min")
    n_err = db.one("SELECT COUNT(*) c FROM errors WHERE run_id=?", (args.run_id,))["c"]
    if n_err:
        _echo(f"Errores: {n_err} (`ros errors {args.run_id}`)")
    if r["report_md"] and r["kind"] == "research":
        _echo(f"Informe: `ros report {args.run_id}`")
    elif r["kind"] == "research" and r["status"] in ("paused", "queued", "failed", "running", "planned"):
        _echo(f"Reanudar: `ros resume {args.run_id}`")
    return EXIT_OK


def cmd_report(args, build_app=None) -> int:
    app = _app(args, build_app)
    if args.output:
        path = _write_report(app, args.run_id, args.output)
        if not path:
            raise UsageError(f"la investigación #{args.run_id} no tiene informe todavía")
        _echo(f"Informe guardado en {path}")
        return EXIT_OK
    row = app.db.one("SELECT report_md FROM runs WHERE id=?", (args.run_id,))
    if not row or not row["report_md"]:
        raise UsageError(f"la investigación #{args.run_id} no tiene informe todavía")
    _echo(row["report_md"])
    return EXIT_OK


def cmd_errors(args, build_app=None) -> int:
    app = _app(args, build_app)
    rows = app.db.all("SELECT * FROM errors WHERE run_id=? ORDER BY id", (args.run_id,))
    if not rows:
        _echo("Sin errores registrados.")
    for e in rows:
        _echo(f"{e['ts'][:19]} {e['kind']:22} {_short(e['subject'], 50)}")
        _echo(f"    {e['message'][:300]}")
        if e["impact"]:
            _echo(f"    → {e['impact']}")
    return EXIT_OK


def cmd_search(args, build_app=None) -> int:
    from .knowledge import search_corpus
    app = _app(args, build_app)
    res = search_corpus(app.db, args.text, args.limit)
    total = sum(len(v) for v in res.values())
    if not total:
        _echo("Sin resultados.")
        return EXIT_OK
    if res["findings"]:
        _echo("Hallazgos:")
        for f in res["findings"]:
            _echo(f"  [#{f['run_id']} {f['kind']}, {f['confidence']}] {f['title']}: {_short(f['summary'], 100)}")
    if res["claims"]:
        _echo("Afirmaciones:")
        for c in res["claims"]:
            _echo(f"  C{c['id']} [{c['claim_type']}, {c['status']}] {_short(c['text'], 100)} — {c['url']}")
    if res["events"]:
        _echo("Eventos de seguimientos:")
        for e in res["events"]:
            _echo(f"  [{e['watch']}] {e['created_at'][:10]} {_short(e['title'], 90)}")
    if res["items"]:
        _echo("Documentos:")
        for i in res["items"]:
            _echo(f"  {_short(i['title'], 60)} — {i['url']}\n      {_short(i['snip'], 140)}")
    return EXIT_OK


def cmd_backup(args, build_app=None) -> int:
    app = _app(args, build_app)
    dest = args.dest or str(ros_home() / "backups" / f"ros-{datetime.now():%Y%m%d-%H%M%S}.db")
    path = app.db.backup(dest)
    _echo(f"Copia verificada (integrity_check ok): {path}")
    return EXIT_OK


# =========================================================================== watches
def _parse_duration(text: str) -> str:
    from .monitor.spec import parse_interval
    t = text.strip().lower()
    minutes = parse_interval(t[:-1] + "d") * 7 if t.endswith("w") else parse_interval(t)
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).replace(microsecond=0).isoformat()


def _apply_flags(spec_data: dict, args) -> dict:
    from .monitor.spec import parse_digest, parse_interval, parse_source
    d = dict(spec_data)
    if args.source:
        sources = [s for s in d.get("sources", [])]
        sources += [parse_source(s).model_dump() for s in args.source]
        d["sources"] = sources
    for rm in getattr(args, "remove_source", []) or []:
        ref = parse_source(rm)
        d["sources"] = [s for s in d.get("sources", []) if (s["kind"], s["locator"]) != (ref.kind, ref.locator)]
    if args.interest:
        d["objective"] = args.interest
    if args.topic:
        d["topics"] = list(dict.fromkeys([*d.get("topics", []), *args.topic]))
    if args.exclude:
        d["exclusions"] = list(dict.fromkeys([*d.get("exclusions", []), *args.exclude]))
    if args.every:
        d["every_minutes"] = parse_interval(args.every)
    if args.digest:
        d["digest"] = parse_digest(args.digest).model_dump()
    alerts = dict(d.get("alerts", {}))
    if args.alert_importance is not None:
        alerts["min_importance"] = args.alert_importance
    if args.alert_sources is not None:
        alerts["min_independent_sources"] = args.alert_sources
    if args.alert_keyword:
        alerts["keywords"] = list(dict.fromkeys([*alerts.get("keywords", []), *args.alert_keyword]))
    if args.no_alerts:
        alerts["enabled"] = False
    if alerts:
        d["alerts"] = alerts
    if args.depth:
        d["depth"] = args.depth
    if args.max_cost is not None:
        d["max_cost_usd_per_run"] = args.max_cost
    if args.tz:
        d["timezone"] = args.tz
    if args.duration:
        d["expires_at"] = _parse_duration(args.duration)
    return d


def _show_draft_and_validate(service, spec, args) -> None:
    _echo(spec.describe())
    _echo("\nComprobando las fuentes…" if not args.no_validate else "")
    for key, state, detail in service.check_sources(spec, probe=not args.no_validate):
        _echo(f"  {'✔' if state == 'ok' else '✖'} {key}: {_short(detail, 100)}")


def _save_new_watch(app, service, spec, *, enable: bool, yes: bool) -> int:
    if not _confirm("\n¿Guardar este seguimiento?", yes):
        _echo("No guardado.")
        return EXIT_OK
    service.create(spec)
    _echo(f"Seguimiento '{spec.name}' guardado (versión 1, desactivado).")
    if enable:
        from .monitor.spec import format_minutes
        if _confirm(f"¿Activarlo ahora? Se ejecutará cada {format_minutes(spec.every_minutes)} mientras `ros daemon` "
                    "esté en marcha.", yes):
            service.set_enabled(spec.name, True)
            _echo("Activado.")
    else:
        _echo(f"Actívalo con `ros watch enable {spec.name}`; pruébalo antes con `ros watch run {spec.name}`.")
    return EXIT_OK


def cmd_watch(args, build_app=None) -> int:
    from .monitor.service import WatchService
    app = _app(args, build_app)
    service = WatchService(app, echo=_echo)
    handler = {"add": _watch_add, "edit": _watch_edit, "list": _watch_list, "show": _watch_show,
               "enable": _watch_enable, "disable": _watch_disable, "run": _watch_run, "history": _watch_history,
               "delete": _watch_delete, "from-research": _watch_from_research}[args.watch_command]
    return handler(app, service, args)


def _watch_add(app, service, args) -> int:
    from pydantic import ValidationError
    from .monitor.drafts import draft_from_text, mark_draft
    from .monitor.spec import WatchSpec, parse_source
    if args.from_text:
        draft = draft_from_text(app, args.from_text, kind="watch", name=args.name,
                                extra_sources=[parse_source(s) for s in args.source])
        _echo(draft.render())
        if draft.spec is None:
            mark_draft(app, draft, "rejected")
            raise UsageError("el borrador está incompleto: añade lo que falta (por ejemplo --source) y repite")
        spec = draft.spec
        flags = argparse.Namespace(**{**vars(args), "source": []})
        spec = WatchSpec.model_validate(_apply_flags(spec.model_dump(), flags))
        _echo("")
        for key, state, detail in service.check_sources(spec, probe=not args.no_validate):
            _echo(f"  {'✔' if state == 'ok' else '✖'} {key}: {_short(detail, 100)}")
        code = _save_new_watch(app, service, spec, enable=args.enable, yes=args.yes)
        mark_draft(app, draft, "applied" if app.db.one("SELECT 1 FROM watches WHERE name=?", (spec.name,)) else "rejected")
        return code
    if not args.name:
        raise UsageError("indica un nombre (ros watch add NOMBRE --source … --interest …) o usa --from-text")
    if not args.interest:
        raise UsageError("describe qué te interesa con --interest")
    data = _apply_flags({"name": args.name, "timezone": app.settings.timezone}, args)
    try:
        spec = WatchSpec.model_validate(data)
    except ValidationError as exc:
        raise UsageError(_validation_message(exc)) from None
    _show_draft_and_validate(service, spec, args)
    return _save_new_watch(app, service, spec, enable=args.enable, yes=args.yes)


def _validation_message(exc) -> str:
    err = exc.errors()[0]
    return f"configuración no válida — {'.'.join(str(p) for p in err['loc'])}: {err['msg']}"


def _watch_edit(app, service, args) -> int:
    from pydantic import ValidationError
    from .monitor.drafts import diff_specs, draft_from_text, mark_draft
    from .monitor.spec import WatchSpec
    row = service.get(args.name)
    current = service.spec_of(row)
    draft = None
    base = current
    if args.from_text:
        draft = draft_from_text(app, args.from_text, kind="watch", current=current)
        if draft.spec is None:
            _echo(draft.render())
            raise UsageError("no se pudo interpretar el cambio")
        base = draft.spec
    try:
        new = WatchSpec.model_validate(_apply_flags(base.model_dump(), args))
    except ValidationError as exc:
        raise UsageError(_validation_message(exc)) from None
    diff = diff_specs(current, new)
    if not diff:
        _echo("Sin cambios.")
        return EXIT_OK
    _echo(diff)
    if draft and draft.warnings:
        _echo("\nAvisos:\n" + "\n".join(f"  - {w}" for w in draft.warnings))
    if not _confirm("\n¿Guardar como nueva versión?", args.yes):
        if draft:
            mark_draft(app, draft, "rejected")
        _echo("Sin cambios.")
        return EXIT_OK
    version = service.update(args.name, new)
    if draft:
        mark_draft(app, draft, "applied")
    _echo(f"Guardada la versión {version} de '{args.name}'.")
    return EXIT_OK


def _watch_list(app, service, args) -> int:
    rows = service.list()
    if not rows:
        _echo("No hay seguimientos. Crea uno con `ros watch add` o `ros draft \"…\"`.")
        return EXIT_OK
    _echo(f"{'nombre':24} {'activo':6} {'v':>3}  {'próxima ejecución':20} {'próximo informe':20} pendientes")
    for w in rows:
        events = app.db.one("SELECT COUNT(*) c FROM events WHERE watch_id=? AND digest_id IS NULL", (w["id"],))["c"]
        _echo(f"{w['name']:24} {'sí' if w['enabled'] else 'no':6} {w['version']:>3}  "
              f"{(w['next_run_at'] or '—')[:16].replace('T', ' '):20} "
              f"{(w['next_digest_at'] or '—')[:16].replace('T', ' '):20} {events} eventos")
    return EXIT_OK


def _watch_show(app, service, args) -> int:
    db = app.db
    row = service.get(args.name)
    spec = service.spec_of(row)
    _echo(spec.describe())
    _echo(f"\nEstado: {'activo' if row['enabled'] else 'desactivado'} · versión {row['version']}")
    _echo(f"Última ejecución: {row['last_run_at'] or '—'} · próxima: {row['next_run_at'] or '—'} · "
          f"próximo informe: {row['next_digest_at'] or '—'}")
    c = db.one("""SELECT (SELECT COUNT(*) FROM watch_items WHERE watch_id=:w) items,
                         (SELECT COUNT(*) FROM watch_items WHERE watch_id=:w AND analysis='pending') pending,
                         (SELECT COUNT(*) FROM events WHERE watch_id=:w) events,
                         (SELECT COUNT(*) FROM events WHERE watch_id=:w AND digest_id IS NULL) undigested,
                         (SELECT COUNT(*) FROM clusters WHERE watch_id=:w) clusters,
                         (SELECT COUNT(*) FROM digests WHERE watch_id=:w) digests""", {"w": row["id"]})
    _echo(f"Items vistos {c['items']} (pendientes {c['pending']}) · eventos {c['events']} "
          f"(sin informar {c['undigested']}) · grupos {c['clusters']} · informes {c['digests']}")
    health = db.all("""SELECT s.kind, s.locator, h.failures, h.open_until, h.last_error, h.last_ok_at FROM sources s
                       JOIN source_health h ON h.source_id=s.id JOIN cursors cu ON cu.source_id=s.id AND cu.watch_id=?""",
                    (row["id"],))
    for h in health:
        state = f"en pausa hasta {h['open_until']}" if h["open_until"] else (
            f"{h['failures']} fallos" if h["failures"] else "ok")
        _echo(f"  fuente {h['kind']}:{_short(h['locator'], 50)} — {state}"
              + (f" ({_short(h['last_error'], 80)})" if h["failures"] else ""))
    return EXIT_OK


def _watch_enable(app, service, args) -> int:
    from .monitor.spec import format_minutes
    spec = service.spec_of(service.get(args.name))
    if not _confirm(f"¿Activar '{args.name}'? Se revisará cada {format_minutes(spec.every_minutes)} con un máximo de "
                    f"${spec.max_cost_usd_per_run:.2f} por ejecución.", args.yes):
        return EXIT_OK
    service.set_enabled(args.name, True)
    _echo("Activado. Lo ejecutará `ros daemon`.")
    return EXIT_OK


def _watch_disable(app, service, args) -> int:
    service.set_enabled(args.name, False)
    _echo("Desactivado. El historial se conserva.")
    return EXIT_OK


def _watch_run(app, service, args) -> int:
    row = service.get(args.name)
    summary = service.run_watch(row["id"], trigger="user")
    pending_events = app.db.one("SELECT COUNT(*) c FROM events WHERE watch_id=? AND digest_id IS NULL", (row["id"],))["c"]
    _echo(f"Estado: {summary['status']}. Eventos sin informar: {pending_events} "
          f"(`ros events {args.name}`, `ros digest {args.name} --now`).")
    return EXIT_FAILED if summary["status"] == "failed" else EXIT_OK


def _watch_history(app, service, args) -> int:
    row = service.get(args.name)
    _echo("Versiones:")
    for v in service.versions(args.name):
        _echo(f"  v{v['version']} · {v['created_at']}")
    _echo("Ejecuciones recientes:")
    for r in app.db.all("SELECT id, kind, status, created_at, outcome_note FROM runs WHERE watch_id=? ORDER BY id DESC "
                        "LIMIT 15", (row["id"],)):
        _echo(f"  #{r['id']} {r['kind']:7} {r['status']:9} {r['created_at'][:16]} {_short(r['outcome_note'], 90)}")
    return EXIT_OK


def _watch_delete(app, service, args) -> int:
    if not _confirm(f"¿Eliminar '{args.name}' con todos sus eventos e informes? (los documentos se conservan)", args.yes):
        return EXIT_OK
    service.delete(args.name)
    _echo("Eliminado.")
    return EXIT_OK


def _watch_from_research(app, service, args) -> int:
    from .monitor.spec import SourceRef, WatchSpec, parse_interval
    db = app.db
    run = db.one("SELECT * FROM runs WHERE id=? AND kind='research'", (args.run_id,))
    if run is None:
        raise UsageError(f"no existe la investigación #{args.run_id}")
    plan = loads(run["plan_json"], {}) or {}
    queries = db.all("SELECT text FROM queries WHERE run_id=? AND status='done' AND results > 0 ORDER BY round_n, id "
                     "LIMIT 3", (args.run_id,))
    primary = db.all("""SELECT DISTINCT i.url FROM run_items ri JOIN items i ON i.id=ri.item_id
                        WHERE ri.run_id=? AND ri.status='extracted' AND i.meta_json LIKE '%"source_kind": "primary"%'
                        LIMIT 5""", (args.run_id,))
    sources = [SourceRef(kind="search", locator=q["text"]) for q in queries]
    sources += [SourceRef(kind="web", locator=p["url"]) for p in primary]
    if not sources:
        raise UsageError("la investigación no tiene consultas ni fuentes primarias que seguir")
    spec = WatchSpec(name=args.name, objective=run["objective"], sources=sources,
                     topics=plan.get("key_terms", [])[:8], every_minutes=parse_interval(args.every),
                     timezone=app.settings.timezone)
    _echo(f"Borrador creado a partir de la investigación #{args.run_id} (la investigación no se modifica):\n")
    _echo(spec.describe())
    return _save_new_watch(app, service, spec, enable=args.enable, yes=args.yes)


def cmd_events(args, build_app=None) -> int:
    from .monitor.digest import TYPE_LABEL
    from .monitor.service import WatchService
    app = _app(args, build_app)
    row = WatchService(app).get(args.name)
    rows = app.db.all("""SELECT e.*, c.title ctitle FROM events e LEFT JOIN clusters c ON c.id=e.cluster_id
                         WHERE e.watch_id=? ORDER BY e.id DESC LIMIT ?""", (row["id"], args.limit))
    if not rows:
        _echo("Sin eventos todavía.")
    for e in rows:
        flags = ("⚠" if e["alerted"] == 1 else " ") + ("✓" if e["digest_id"] else " ")
        _echo(f"{flags} #{e['id']:<5} {e['created_at'][:16].replace('T', ' ')} imp {e['importance']:.2f} "
              f"[{TYPE_LABEL.get(e['claim_type'], e['claim_type'])}] {_short(e['title'], 80)}")
        if e["cluster_id"] and e["ctitle"] != e["title"]:
            _echo(f"          grupo #{e['cluster_id']}: {_short(e['ctitle'], 80)}")
    _echo("\n⚠ = alerta enviada · ✓ = incluido en un informe")
    return EXIT_OK


def cmd_digest(args, build_app=None) -> int:
    from .monitor.digest import build_digest
    from .monitor.schedule import SystemClock
    from .monitor.service import WatchService
    app = _app(args, build_app)
    row = WatchService(app).get(args.name)
    if args.list:
        for d in app.db.all("SELECT id, created_at, period_start, period_end, event_count FROM digests WHERE watch_id=? "
                            "ORDER BY id DESC", (row["id"],)):
            _echo(f"#{d['id']} {d['period_start'][:16]} → {d['period_end'][:16]} · {d['event_count']} eventos")
        return EXIT_OK
    digest_id = args.show
    if args.now:
        digest_id = build_digest(app, row["id"], now=SystemClock().now(), force=True, echo=_echo)
        if digest_id is None:
            _echo("No hay eventos nuevos desde el último informe: no se genera uno vacío.")
            return EXIT_OK
    if digest_id is None:
        last = app.db.one("SELECT id FROM digests WHERE watch_id=? ORDER BY id DESC LIMIT 1", (row["id"],))
        if not last:
            _echo(f"Todavía no hay informes. Genera uno con `ros digest {args.name} --now`.")
            return EXIT_OK
        digest_id = last["id"]
    d = app.db.one("SELECT markdown FROM digests WHERE id=? AND watch_id=?", (digest_id, row["id"]))
    if d is None:
        raise UsageError(f"no existe el informe #{digest_id} de {args.name}")
    if args.output:
        Path(args.output).write_text(d["markdown"], encoding="utf-8")
        _echo(f"Informe guardado en {args.output}")
    else:
        _echo(d["markdown"])
    return EXIT_OK


def cmd_inbox(args, build_app=None) -> int:
    app = _app(args, build_app)
    db = app.db
    if args.read_all:
        n = db.execute("UPDATE notifications SET read=1 WHERE read=0").rowcount
        _echo(f"{n} marcadas como leídas.")
        return EXIT_OK
    if args.show:
        n = db.one("SELECT * FROM notifications WHERE id=?", (args.show,))
        if n is None:
            raise UsageError(f"no existe la notificación #{args.show}")
        db.execute("UPDATE notifications SET read=1 WHERE id=?", (args.show,))
        _echo(f"{n['title']}\n{n['ts']} · {n['kind']}\n\n{n['body']}")
        return EXIT_OK
    rows = db.all(f"""SELECT n.*, w.name watch FROM notifications n LEFT JOIN watches w ON w.id=n.watch_id
                      {'' if args.all else 'WHERE n.read=0'} ORDER BY n.id DESC LIMIT 50""")
    if not rows:
        _echo("Bandeja vacía." if args.all else "No hay notificaciones sin leer.")
        return EXIT_OK
    icon = {"alert": "⚠", "digest": "🗞", "attention": "✋"}
    for n in rows:
        _echo(f"{'●' if not n['read'] else ' '} #{n['id']:<5} {n['ts'][:16].replace('T', ' ')} {icon.get(n['kind'], '·')} "
              f"{_short(n['title'], 80)}")
    _echo("\nLee una con `ros inbox --show ID`.")
    return EXIT_OK


def cmd_draft(args, build_app=None) -> int:
    from .monitor.drafts import draft_from_text, mark_draft
    from .monitor.service import WatchService
    from .monitor.spec import parse_source
    app = _app(args, build_app)
    draft = draft_from_text(app, args.text, name=args.name, extra_sources=[parse_source(s) for s in args.source])
    _echo(draft.render())
    if draft.kind == "research" and draft.research:
        from .budget import Budget
        from .research.engine import ResearchEngine
        r = draft.research
        if not _confirm("\n¿Crear y planificar esta investigación?", args.yes):
            mark_draft(app, draft, "rejected")
            return EXIT_OK
        engine = ResearchEngine(app, echo=_echo)
        run_id = engine.create(r["objective"], Budget.from_dict(r["budget"]), focus=r["focus"],
                               exclude_domains=tuple(r["exclude_domains"]))
        mark_draft(app, draft, "applied")
        plan = engine.prepare(run_id)
        run = app.db.one("SELECT config_json, budget_json FROM runs WHERE id=?", (run_id,))
        _render_plan(plan, loads(run["config_json"]), loads(run["budget_json"]))
        if not _confirm("\n¿Ejecutar la investigación con este plan?", args.yes):
            engine.cancel(run_id)
            return EXIT_OK
        return _run_research(app, engine, run_id, None)
    if draft.spec is None:
        mark_draft(app, draft, "rejected")
        raise UsageError("el borrador está incompleto: añade lo que falta (por ejemplo --source) y repite")
    service = WatchService(app, echo=_echo)
    code = _save_new_watch(app, service, draft.spec, enable=args.enable, yes=args.yes)
    mark_draft(app, draft, "applied" if app.db.one("SELECT 1 FROM watches WHERE name=?", (draft.spec.name,))
               else "rejected")
    return code


def cmd_daemon(args, build_app=None) -> int:
    from .monitor.daemon import Daemon
    app = _app(args, build_app)
    daemon = Daemon(app, echo=_echo)
    if args.once:
        done = daemon.tick()
        daemon.release()
        _echo("Ciclo completado: " + ", ".join(f"{k} {v}" for k, v in done.items()))
        return EXIT_OK
    _echo(f"ros daemon en marcha (ciclo cada {args.poll:g} s). Ctrl+C para detenerlo limpiamente.")
    daemon.serve(poll_seconds=args.poll)
    _echo("Daemon detenido.")
    return EXIT_OK


def cmd_connectors(args, build_app=None) -> int:
    app = _app(args, build_app)
    _echo(f"{'conector':12} {'acceso':13} {'estado':18} detalle")
    for cid, c in sorted(app.connectors.items()):
        st = c.probe() if args.probe and hasattr(c, "probe") else c.status()
        cap = c.capabilities()
        _echo(f"{cid:12} {cap.access_mode:13} {st.state:18} {_short(st.detail, 70)}")
        _echo(f"{'':12} {_short(cap.notes, 110)}")
    return EXIT_OK


def cmd_sources(args, build_app=None) -> int:
    from .knowledge import source_reputation
    app = _app(args, build_app)
    rows = source_reputation(app.db)[: args.limit]
    if not rows:
        _echo("Todavía no hay fuentes analizadas.")
        return EXIT_OK
    _echo(f"{'fuente':32} {'docs':>5} {'tipo':11} {'autoridad':>9} {'exactitud':>9} {'primaria':>8} {'conflictos':>10} "
          f"{'puntuación':>10}")
    for r in rows:
        _echo(f"{_short(r['host'], 32):32} {r['docs']:>5} {r['kind']:11} {r['authority']:>9.2f} {r['accuracy']:>9.2f} "
              f"{r['primary_share']:>8.0%} {r['conflicts_of_interest']:>10} {r['score']:>10.2f}"
              + ("  ⚠ inyección" if r["injection_attempts"] else ""))
    return EXIT_OK


def cmd_feedback(args, build_app=None) -> int:
    from .knowledge import record_feedback
    app = _app(args, build_app)
    fid = record_feedback(app.db, args.target, args.target_id, args.value, args.note)
    _echo(f"Feedback #{fid} registrado. Afecta a la prioridad futura; no borra nada del historial.")
    return EXIT_OK


def cmd_prune(args, build_app=None) -> int:
    from .knowledge import prune
    app = _app(args, build_app)
    preview = prune(app.db, args.older_than, dry_run=True)
    _echo(f"Items con texto anterior a {preview['cutoff'][:10]}: {preview['items']} "
          f"({preview['chars']} caracteres, {preview['snapshots']} versiones). Se conservan metadatos, hashes, "
          "afirmaciones y enlaces.")
    if args.dry_run or not preview["items"]:
        return EXIT_OK
    if not _confirm("¿Borrar ese texto completo?", args.yes):
        return EXIT_OK
    done = prune(app.db, args.older_than)
    _echo(f"Hecho: {done['items']} items podados (registrado en prune_log).")
    return EXIT_OK


def cmd_serve(args, build_app=None) -> int:  # pragma: no cover - runs a server
    import uvicorn
    from .api import create_app, load_or_create_token
    app = _app(args, build_app)
    host = args.host or app.settings.api_host
    port = args.port or app.settings.api_port
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise UsageError("por seguridad la API solo escucha en la interfaz local (127.0.0.1)")
    api = create_app(app, token=load_or_create_token())
    code = api.state.new_login_code()
    _echo(f"API local en http://{host}:{port}/api/v1 (token en {ros_home() / 'api_token'})")
    _echo(f"Panel: http://{host}:{port}/login?code={code}  (enlace de un solo uso)")
    uvicorn.run(api, host=host, port=port, log_level="warning")
    return EXIT_OK


def cmd_obsidian(args, build_app=None) -> int:
    from .obsidian import export_vault
    app = _app(args, build_app)
    vault = args.vault or app.settings.obsidian_vault
    if not vault:
        raise UsageError("indica el vault (ros obsidian RUTA) o define obsidian_vault en ros.toml")
    res = export_vault(app.db, vault, dry_run=args.dry_run)
    prefix = "(simulación) " if args.dry_run else ""
    _echo(f"{prefix}Escritas {len(res.written)} notas · sin cambios {len(res.unchanged)} · conflictos {len(res.conflicts)}")
    for c in res.conflicts:
        _echo(f"  ⚠ editaste el bloque gestionado de {c}: se escribió una copia .ros-conflict.md al lado")
    return EXIT_OK


def cmd_deliver(args, build_app=None) -> int:
    from .notify import Notifier
    app = _app(args, build_app)
    notifier = Notifier(app)
    if not notifier.channels():
        _echo("No hay canales configurados (notify_webhook_url, notify_telegram_chat_id o notify_email_to).")
        return EXIT_OK
    _echo(f"Enviadas: {notifier.deliver_pending()}")
    return EXIT_OK


COMMANDS = {"research": cmd_research, "runs": cmd_runs, "show": cmd_show, "resume": cmd_resume,
            "cancel": cmd_cancel, "report": cmd_report, "errors": cmd_errors, "search": cmd_search,
            "backup": cmd_backup, "watch": cmd_watch, "events": cmd_events, "digest": cmd_digest,
            "inbox": cmd_inbox, "draft": cmd_draft, "daemon": cmd_daemon, "connectors": cmd_connectors,
            "sources": cmd_sources, "feedback": cmd_feedback, "prune": cmd_prune, "serve": cmd_serve,
            "obsidian": cmd_obsidian, "deliver": cmd_deliver}


def _sigterm_as_interrupt(command: str) -> None:
    """SIGTERM (kill, timeout, systemd) pauses like Ctrl+C: progress saved, lock released, resumable.
    The daemon installs its own graceful handler."""
    import signal
    if command == "daemon":
        return

    def handler(signum, frame):
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, handler)
    except ValueError:  # not in the main thread
        pass


def main(argv: list[str] | None = None, *, build_app=None) -> int:
    """`build_app(settings) -> App` can be injected (tests use fakes for network and model)."""
    args = _parser().parse_args(argv)
    _sigterm_as_interrupt(args.command)
    try:
        return COMMANDS[args.command](args, build_app)
    except NeedsUserAction as exc:
        print(f"requiere tu intervención: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except RosError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILED if not exc.fatal else EXIT_USAGE
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        print("\ninterrumpido", file=sys.stderr)
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
