"""Alerts (explicit rules only) and cumulative digests.

Digests are idempotent: an event belongs to at most one digest, so generating twice with no new
events produces nothing. Notifications carry a unique dedupe key.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from ..budget import Budget, Ledger
from ..connectors.feeds import host_of
from ..db import Database, dumps, loads, now_iso
from ..errors import BudgetExhausted, RosError
from ..registry import App
from . import prompts
from .correlate import independent_hosts
from .schedule import iso
from .spec import WatchSpec

TYPE_LABEL = {"fact": "hecho", "inference": "inferencia", "opinion": "opinión", "rumor": "rumor (no confirmado)",
              "prediction": "predicción"}
CONF = {"high": "alta", "medium": "media", "low": "baja"}


# ====================================================================== alerts
def evaluate_alerts(db: Database, watch_id: int, spec: WatchSpec, *, now: datetime) -> list[int]:
    rules = spec.alerts
    if not rules.enabled:
        return []
    day = now.date().isoformat()
    sent_today = db.one("SELECT COUNT(*) c FROM notifications WHERE watch_id=? AND kind='alert' AND substr(ts,1,10)=?",
                        (watch_id, day))["c"]
    created: list[int] = []
    keywords = [k.lower() for k in rules.keywords if k.strip()]

    def emit(key: str, title: str, body: str, ref_kind: str, ref_id: int, event_ids: list[int]) -> None:
        nonlocal sent_today
        if db.one("SELECT 1 FROM notifications WHERE dedupe_key=?", (key,)):
            return
        if sent_today >= rules.max_per_day:
            db.execute(f"UPDATE events SET alerted=2 WHERE alerted=0 AND id IN ({','.join('?' * len(event_ids))})",
                       event_ids)
            return
        with db.tx():
            cur = db.execute("""INSERT OR IGNORE INTO notifications (watch_id, ts, kind, title, body, dedupe_key, ref_kind, ref_id)
                                VALUES (?,?,?,?,?,?,?,?)""", (watch_id, now_iso(), "alert", title, body, key, ref_kind, ref_id))
            if cur.rowcount:
                created.append(cur.lastrowid)
                sent_today += 1
            db.execute(f"UPDATE events SET alerted=1 WHERE id IN ({','.join('?' * len(event_ids))})", event_ids)

    for ev in db.all("SELECT * FROM events WHERE watch_id=? AND alerted=0 ORDER BY importance DESC, id", (watch_id,)):
        text = f"{ev['title']} {ev['summary']}".lower()
        reasons = []
        if ev["importance"] >= rules.min_importance:
            reasons.append(f"importancia {ev['importance']:.2f} ≥ {rules.min_importance:g}")
        hit = [k for k in keywords if k in text]
        if hit:
            reasons.append("menciona: " + ", ".join(hit))
        if reasons:
            emit(f"alert:event:{ev['id']}", f"⚠ {spec.name}: {ev['title']}", _event_body(db, ev, reasons),
                 "event", ev["id"], [ev["id"]])
    if rules.min_independent_sources:
        for c in db.all("SELECT * FROM clusters WHERE watch_id=? AND status='open'", (watch_id,)):
            hosts = independent_hosts(db, c["id"])
            if len(hosts) >= rules.min_independent_sources:
                ids = [r["id"] for r in db.all("SELECT id FROM events WHERE cluster_id=?", (c["id"],))]
                reason = f"{len(hosts)} fuentes independientes informan de lo mismo ({', '.join(sorted(hosts))})"
                body = f"{c['summary'] or ''}\n\nMotivo de la alerta: {reason}\n"
                body += "\n".join(f"- {r['title']}" for r in db.all("SELECT title FROM events WHERE cluster_id=?", (c["id"],)))
                emit(f"alert:cluster:{c['id']}", f"⚠ {spec.name}: {c['title']}", body, "cluster", c["id"], ids)
    return created


def _event_body(db: Database, ev, reasons: list[str]) -> str:
    links = db.all("SELECT i.url, i.title FROM event_items ei JOIN items i ON i.id=ei.item_id WHERE ei.event_id=?",
                   (ev["id"],))
    lines = [ev["summary"], "", f"Tipo: {TYPE_LABEL.get(ev['claim_type'], ev['claim_type'])} · "
             f"relevancia {ev['relevance']:.2f} · importancia {ev['importance']:.2f}"]
    if ev["why"]:
        lines.append(f"Por qué importa: {ev['why']}")
    lines.append("Motivo de la alerta: " + "; ".join(reasons))
    lines += [f"- {l['url']}" for l in links if l["url"]]
    return "\n".join(lines)


# ====================================================================== digests
def build_digest(app: App, watch_id: int, *, now: datetime, force: bool = False,
                 echo: Callable[[str], None] = lambda s: None) -> int | None:
    """Create the digest for every event not yet reported. Returns the digest id, or None if nothing to report."""
    db = app.db
    watch = db.one("SELECT * FROM watches WHERE id=?", (watch_id,))
    spec = WatchSpec.model_validate_json(watch["spec_json"])
    events = db.all("SELECT * FROM events WHERE watch_id=? AND digest_id IS NULL ORDER BY created_at, id", (watch_id,))
    if not events or (len(events) < spec.digest.min_events and not force):
        return None
    period_start = watch["last_digest_at"] or min(e["created_at"] for e in events)
    period_end = iso(now)
    from .service import WatchService
    allowance = WatchService(app, clock=_Fixed(now)).daily_allowance()
    budget = Budget(max_rounds=1, max_minutes=20, max_tokens=1_000_000, max_cost_usd=max(0.0, min(1.0, allowance)),
                    final_reserve=0.0)
    ts = now_iso()
    run_id = db.insert("runs", {"kind": "digest", "objective": spec.objective, "status": "running", "stage": "reporting",
                                "watch_id": watch_id, "trigger": "scheduler", "budget_json": dumps(budget.to_dict()),
                                "created_at": ts, "updated_at": ts})
    ledger = Ledger(db, run_id, budget)
    groups = _groups(db, events)
    previous = db.one("SELECT id, summary, period_end FROM digests WHERE watch_id=? ORDER BY id DESC LIMIT 1", (watch_id,))
    errors = db.all("""SELECT kind, subject, COUNT(*) n, MAX(message) message FROM errors
                       WHERE watch_id=? AND ts >= ? GROUP BY kind, subject ORDER BY n DESC""", (watch_id, period_start))
    synthesis, failure = None, None
    if budget.max_cost_usd > 0:
        try:
            synthesis = _synthesize(app, spec, groups, previous, errors, ledger)
        except BudgetExhausted as exc:
            failure = exc.message
        except RosError as exc:
            if exc.fatal:
                raise
            failure = str(exc)
    else:
        failure = "límite diario de coste alcanzado"
    if failure:
        db.record_error(run_id=run_id, watch_id=watch_id, kind="model_error", message=failure,
                        impact="digest generado sin síntesis del modelo (solo eventos agrupados)")
    md = render_digest(db, spec, groups, synthesis=synthesis, period=(period_start, period_end), previous=previous,
                       errors=errors, failure=failure, total=len(events))
    ids = [e["id"] for e in events]
    key = f"digest:{watch_id}:{ids[0]}-{ids[-1]}:{len(ids)}"
    summary = (synthesis or {}).get("executive_summary") or f"{len(events)} eventos en {len(groups)} grupos."
    with db.tx():
        still = db.one(f"SELECT COUNT(*) c FROM events WHERE digest_id IS NULL AND id IN ({','.join('?' * len(ids))})",
                       ids)["c"]
        if still != len(ids) or db.one("SELECT 1 FROM digests WHERE dedupe_key=?", (key,)):
            ledger.checkpoint_time()
            db.execute("UPDATE runs SET status='cancelled', stage='done', outcome_note='digest ya generado' WHERE id=?",
                       (run_id,))
            return None
        digest_id = db.insert("digests", {"watch_id": watch_id, "created_at": now_iso(), "period_start": period_start,
                                          "period_end": period_end, "summary": summary[:2000], "markdown": md,
                                          "event_count": len(events), "dedupe_key": key,
                                          "data_json": dumps(synthesis) if synthesis else None})
        db.execute(f"UPDATE events SET digest_id=? WHERE id IN ({','.join('?' * len(ids))})", (digest_id, *ids))
        db.execute("UPDATE watches SET last_digest_at=? WHERE id=?", (period_end, watch_id))
        title = (synthesis or {}).get("title") or f"Informe de {spec.name}"
        db.execute("""INSERT OR IGNORE INTO notifications (watch_id, ts, kind, title, body, dedupe_key, ref_kind, ref_id)
                      VALUES (?,?,?,?,?,?,?,?)""", (watch_id, now_iso(), "digest", f"🗞 {title}", md,
                                                    f"digest:{digest_id}", "digest", digest_id))
        db.execute("UPDATE runs SET status=?, stage='done', finished_at=?, outcome_note=? WHERE id=?",
                   ("partial" if failure else "completed", now_iso(), f"digest #{digest_id}", run_id))
    ledger.checkpoint_time()
    echo(f"🗞 digest #{digest_id} de {spec.name}: {len(events)} eventos")
    return digest_id


class _Fixed:
    def __init__(self, now: datetime):
        self._now = now

    def now(self) -> datetime:
        return self._now


def _groups(db: Database, events) -> list[dict]:
    by_cluster: dict[int, list] = {}
    for e in events:
        by_cluster.setdefault(e["cluster_id"] or -e["id"], []).append(e)
    groups = []
    for cid, evs in by_cluster.items():
        cluster = db.one("SELECT * FROM clusters WHERE id=?", (cid,)) if cid > 0 else None
        analysis = loads(cluster["analysis_json"], {}) if cluster and cluster["analysis_json"] else {}
        sources = []
        for e in evs:
            for r in db.all("""SELECT i.url, i.title, i.published_at, ei.relation, s.kind skind FROM event_items ei
                               JOIN items i ON i.id=ei.item_id LEFT JOIN sources s ON s.id=i.source_id
                               WHERE ei.event_id=?""", (e["id"],)):
                sources.append({**dict(r), "event_id": e["id"], "host": host_of(r["url"])})
        importance = max([e["importance"] for e in evs] + [analysis.get("importance", 0)])
        groups.append({"id": len(groups) + 1, "cluster_id": cid if cid > 0 else None,
                       "title": cluster["title"] if cluster and len(evs) > 1 else evs[0]["title"],
                       "summary": cluster["summary"] if cluster and len(evs) > 1 else evs[0]["summary"],
                       "analysis": analysis, "events": evs, "sources": sources, "importance": importance,
                       "hosts": sorted({s["host"] for s in sources} - {""})})
    groups.sort(key=lambda g: (-g["importance"], -len(g["events"])))
    for k, g in enumerate(groups, 1):
        g["id"] = k
    return groups


def _synthesize(app: App, spec: WatchSpec, groups: list[dict], previous, errors, ledger: Ledger) -> dict:
    lines = []
    for g in groups[: spec.digest.max_events]:
        a = g["analysis"]
        lines.append(f"G{g['id']} — {g['title']} (importance {g['importance']:.2f}, sources: {', '.join(g['hosts']) or '?'})")
        if a:
            lines.append(f"  relation: {a.get('relation', '')}; confirmed: {a.get('confirmed', [])}; "
                         f"uncertain: {a.get('uncertain', [])}; contradictions: {a.get('contradictions', [])}")
        for e in g["events"]:
            lines.append(f"  - [{e['claim_type']}, {e['created_at'][:16]}] {e['title']}: {e['summary']}")
    problems = "; ".join(f"{e['kind']}×{e['n']} {e['subject'] or ''}" for e in errors) or "none"
    user = (f"Watch interest:\n{spec.objective}\n\nTopics: {', '.join(spec.topics) or '(any)'}\n\n"
            f"Previous report summary: {previous['summary'] if previous else '(none: this is the first report)'}\n\n"
            f"Collection problems in the period: {problems}\n\n"
            f"Grouped events (untrusted summaries):\n<events>\n" + "\n".join(lines) + "\n</events>")
    return app.llm(prompts.STAGE_ROLE["digest"]).complete_json(
        purpose="digest", system=prompts.DIGEST_SYSTEM, user=user, schema=prompts.DIGEST_SCHEMA,
        max_tokens=prompts.MAX_TOKENS["digest"], ledger=ledger)


def render_digest(db: Database, spec: WatchSpec, groups: list[dict], *, synthesis: dict | None, period: tuple[str, str],
                  previous, errors, failure: str | None, total: int) -> str:
    out = [f"# {(synthesis or {}).get('title') or 'Informe de seguimiento: ' + spec.name}\n",
           f"> **Seguimiento:** {spec.name} — {spec.objective}  ",
           f"> **Periodo:** {period[0][:16].replace('T', ' ')} → {period[1][:16].replace('T', ' ')} UTC · "
           f"{total} eventos en {len(groups)} grupos  "]
    if synthesis:
        out.append(f"> **Confianza:** {CONF.get(synthesis['confidence'], synthesis['confidence'])} — "
                   f"{synthesis['confidence_note']}")
    if failure:
        out.append(f"> **Limitación:** síntesis del modelo no disponible ({failure}); se listan los eventos agrupados.")
    out.append("")
    if synthesis:
        out += ["## Resumen ejecutivo\n", synthesis["executive_summary"], ""]
        if synthesis["developments"]:
            out.append("## Acontecimientos principales\n")
            for d in synthesis["developments"]:
                out.append(f"- **{d['headline']}** (grupo {d['group_id']}): {d['body']}")
            out.append("")
    out.append("## Grupos temáticos\n")
    shown = groups[: spec.digest.max_events]
    for g in shown:
        a = g["analysis"]
        out.append(f"### {g['id']}. {g['title']}\n")
        out.append(f"{g['summary']}\n")
        meta = [f"importancia {g['importance']:.2f}", f"{len(g['events'])} eventos",
                f"{len(g['hosts'])} fuentes independientes"]
        out.append("_" + " · ".join(meta) + "_\n")
        if a.get("relation"):
            out.append(f"**Relación:** {a['relation']}  ")
        if a.get("confirmed"):
            out.append("**Confirmado:** " + "; ".join(a["confirmed"]) + "  ")
        if a.get("uncertain"):
            out.append("**Sin confirmar:** " + "; ".join(a["uncertain"]) + "  ")
        if a.get("contradictions"):
            out.append("**Contradicciones:** " + "; ".join(a["contradictions"]) + "  ")
        if a.get("why_relevant"):
            out.append(f"**Por qué importa:** {a['why_relevant']}  ")
        out.append("")
        for e in g["events"]:
            label = TYPE_LABEL.get(e["claim_type"], e["claim_type"])
            mod = " _(modificado)_" if e["change"] == "modified" else ""
            out.append(f"- **{e['title']}**{mod} — {label}. {e['summary']}")
            for s in [s for s in g["sources"] if s["event_id"] == e["id"]]:
                rel = "" if s["relation"] == "primary" else " (copia en otra fuente)"
                out.append(f"  - [{s['host'] or s['skind']}]({s['url']}){rel}" if s["url"] else f"  - {s['title']}{rel}")
        out.append("")
    if len(groups) > len(shown):
        out.append(f"_… y {len(groups) - len(shown)} grupos más de menor importancia (`ros events {spec.name}`)._\n")

    out.append("## Cronología\n")
    timeline = sorted((e for g in groups for e in g["events"]), key=lambda e: e["created_at"])
    for e in timeline:
        item = db.one("SELECT published_at FROM items WHERE id=?", (e["item_id"],))
        when = (item["published_at"] or e["created_at"])[:16].replace("T", " ")
        out.append(f"- {when} — {e['title']}")
    out.append("")
    if synthesis:
        for key, heading in (("connections", "Conexiones detectadas"), ("contradictions", "Contradicciones"),
                             ("changes_since_previous", "Novedades respecto al informe anterior"),
                             ("implications", "Posibles implicaciones"), ("open_questions", "Preguntas abiertas"),
                             ("watch_next", "Qué seguirá vigilando ROS")):
            if synthesis[key]:
                out.append(f"## {heading}\n")
                out += [f"- {x}" for x in synthesis[key]]
                out.append("")
        if not synthesis["changes_since_previous"] and previous:
            out += ["## Novedades respecto al informe anterior\n", f"Informe anterior: #{previous['id']} "
                    f"(hasta {previous['period_end'][:16].replace('T', ' ')}).", ""]
    out.append("## Fuentes\n")
    out.append("| Fuente | Tipo | Enlace |")
    out.append("|---|---|---|")
    seen = set()
    for g in groups:
        for s in g["sources"]:
            if s["url"] and s["url"] not in seen:
                seen.add(s["url"])
                out.append(f"| {s['host'] or '—'} | {s['skind'] or '—'} | [{(s['title'] or s['url'])[:70].replace('|', '/')}]({s['url']}) |")
    out.append("")
    out.append("## Cobertura\n")
    pending = db.one("SELECT COUNT(*) c FROM watch_items wi JOIN watches w ON w.id=wi.watch_id WHERE w.name=? AND "
                     "wi.analysis='pending'", (spec.name,))["c"]
    if errors:
        out.append("Problemas de recopilación en el periodo:\n")
        out += [f"- **{e['kind']}** {e['subject'] or ''} ×{e['n']}: {e['message'][:160]}" for e in errors]
    else:
        out.append("Todas las fuentes respondieron en el periodo.")
    if pending:
        out.append(f"\n{pending} items siguen pendientes de análisis y aparecerán en el próximo informe.")
    out.append("")
    return "\n".join(out)
