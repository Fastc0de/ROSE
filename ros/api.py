"""Local API and read-only dashboard (`ros serve`).

- Binds to 127.0.0.1 by default; the Host header is validated (DNS-rebinding defence).
- Every endpoint requires authentication: `Authorization: Bearer <token>` (token in
  $ROS_HOME/api_token, created with owner-only permissions), or a dashboard session cookie obtained
  through a single-use login code printed by `ros serve`. Tokens never travel in URLs.
- Cookie sessions are read-only: mutations require the bearer token, so CSRF cannot trigger them.
- Handlers never run connectors or models inline: a research request is queued for the daemon.
"""

from __future__ import annotations

import hmac
import html
import os
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field

from .budget import Budget
from .config import ros_home
from .db import loads, now_iso
from .knowledge import search_corpus
from .registry import App

SESSION_COOKIE = "ros_session"


def load_or_create_token(home: Path | None = None) -> str:
    """The local API secret: created once with owner-only permissions, reused afterwards."""
    home = home or ros_home()
    home.mkdir(parents=True, exist_ok=True)
    path = home / "api_token"
    if path.exists():
        return path.read_text().strip()
    token = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    return token


class ResearchRequest(BaseModel):
    objective: str = Field(min_length=3, max_length=2000)
    focus: str = ""
    exclude_domains: list[str] = []
    max_cost_usd: float | None = Field(None, gt=0, le=50)
    max_rounds: int | None = Field(None, ge=1, le=12)


def create_app(app: App, *, token: str, allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost")) -> FastAPI:
    api = FastAPI(title="ROS", version="1", docs_url=None, redoc_url=None, openapi_url=None)
    sessions: set[str] = set()
    login_codes: set[str] = set()
    api.state.login_codes = login_codes
    db = app.db

    def new_login_code() -> str:
        code = secrets.token_urlsafe(16)
        login_codes.add(code)
        return code

    api.state.new_login_code = new_login_code

    @api.middleware("http")
    async def host_guard(request: Request, call_next):
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in allowed_hosts:
            return PlainTextResponse("host no permitido", status_code=400)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    def bearer_ok(authorization: str | None) -> bool:
        if not authorization or not authorization.lower().startswith("bearer "):
            return False
        return hmac.compare_digest(authorization[7:].strip().encode(), token.encode())

    def reader(request: Request, authorization: str | None = Header(None)) -> None:
        if bearer_ok(authorization) or request.cookies.get(SESSION_COOKIE) in sessions:
            return
        raise HTTPException(401, "autenticación requerida")

    def writer(authorization: str | None = Header(None)) -> None:
        if not bearer_ok(authorization):
            raise HTTPException(401, "las modificaciones requieren el token (Authorization: Bearer)")

    # ------------------------------------------------------------------ session bootstrap
    @api.get("/login")
    def login(code: str = Query(...)):
        if code not in login_codes:
            raise HTTPException(403, "código de acceso no válido o ya usado")
        login_codes.discard(code)
        sid = secrets.token_urlsafe(32)
        sessions.add(sid)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(SESSION_COOKIE, sid, httponly=True, samesite="strict")
        return resp

    # ------------------------------------------------------------------ API
    @api.get("/api/v1/health", dependencies=[Depends(reader)])
    def health():
        version = db.one("SELECT MAX(version) v FROM schema_migrations")["v"]
        daemon = db.one("SELECT owner, expires_at FROM locks WHERE name='daemon'")
        pending = db.one("SELECT COUNT(*) c FROM watch_items WHERE analysis='pending'")["c"]
        open_circuits = db.one("SELECT COUNT(*) c FROM source_health WHERE open_until > ?", (now_iso(),))["c"]
        queued = db.one("SELECT COUNT(*) c FROM runs WHERE status IN ('queued','running')")["c"]
        return {"status": "ok", "schema_version": version, "daemon": dict(daemon) if daemon else None,
                "pending_analysis": pending, "open_circuits": open_circuits, "active_runs": queued}

    @api.get("/api/v1/runs", dependencies=[Depends(reader)])
    def runs(kind: str | None = None, limit: int = Query(50, ge=1, le=200), before: int | None = None):
        sql, params = "SELECT id, kind, objective, status, stage, watch_id, created_at, finished_at, outcome_note " \
                      "FROM runs WHERE 1=1", []
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        if before:
            sql += " AND id < ?"
            params.append(before)
        rows = [dict(r) for r in db.all(sql + " ORDER BY id DESC LIMIT ?", (*params, limit))]
        return {"items": rows, "next": rows[-1]["id"] if len(rows) == limit else None}

    def _run(run_id: int):
        row = db.one("SELECT * FROM runs WHERE id=?", (run_id,))
        if row is None:
            raise HTTPException(404, "no existe")
        return row

    @api.get("/api/v1/runs/{run_id}", dependencies=[Depends(reader)])
    def run_detail(run_id: int):
        r = _run(run_id)
        usage = db.one("SELECT COALESCE(SUM(cost_usd),0) cost, COALESCE(SUM(input_tokens+output_tokens),0) tokens "
                       "FROM usage WHERE run_id=?", (run_id,))
        return {"id": r["id"], "kind": r["kind"], "objective": r["objective"], "status": r["status"],
                "stage": r["stage"], "created_at": r["created_at"], "finished_at": r["finished_at"],
                "outcome_note": r["outcome_note"], "plan": loads(r["plan_json"]), "cost_usd": usage["cost"],
                "tokens": usage["tokens"],
                "rounds": [dict(x) for x in db.all("SELECT n, stage, started_at, finished_at FROM rounds WHERE run_id=?",
                                                   (run_id,))]}

    @api.get("/api/v1/runs/{run_id}/report", dependencies=[Depends(reader)])
    def run_report(run_id: int):
        r = _run(run_id)
        if not r["report_md"] or r["kind"] != "research":
            raise HTTPException(404, "sin informe")
        return PlainTextResponse(r["report_md"], media_type="text/markdown; charset=utf-8")

    @api.get("/api/v1/runs/{run_id}/errors", dependencies=[Depends(reader)])
    def run_errors(run_id: int):
        _run(run_id)
        return {"items": [dict(e) for e in db.all("SELECT ts, kind, subject, message, impact FROM errors WHERE run_id=?",
                                                  (run_id,))]}

    @api.post("/api/v1/runs", status_code=202, dependencies=[Depends(writer)])
    def submit_run(body: ResearchRequest, idempotency_key: str | None = Header(None)):
        if idempotency_key:
            row = db.one("SELECT resource_id FROM idempotency WHERE key=?", (idempotency_key,))
            if row:
                return JSONResponse({"id": row["resource_id"], "status": "exists"}, status_code=200)
        from .research.engine import ResearchEngine
        budget = Budget.from_dict({**app.settings.budget.to_dict(),
                                   **({"max_cost_usd": body.max_cost_usd} if body.max_cost_usd else {}),
                                   **({"max_rounds": body.max_rounds} if body.max_rounds else {})})
        run_id = ResearchEngine(app).create(body.objective, budget, focus=body.focus,
                                            exclude_domains=tuple(body.exclude_domains), status="queued", trigger="api")
        if idempotency_key:
            db.execute("INSERT OR IGNORE INTO idempotency (key, kind, resource_id, created_at) VALUES (?,?,?,?)",
                       (idempotency_key, "run", run_id, now_iso()))
        return {"id": run_id, "status": "queued", "note": "la ejecuta `ros daemon`"}

    @api.post("/api/v1/runs/{run_id}/cancel", dependencies=[Depends(writer)])
    def cancel_run(run_id: int):
        _run(run_id)
        from .research.engine import ResearchEngine
        return {"id": run_id, "status": ResearchEngine(app).cancel(run_id)}

    @api.get("/api/v1/watches", dependencies=[Depends(reader)])
    def watches():
        return {"items": [{"name": w["name"], "enabled": bool(w["enabled"]), "version": w["version"],
                           "next_run_at": w["next_run_at"], "next_digest_at": w["next_digest_at"],
                           "last_run_at": w["last_run_at"], "spec": loads(w["spec_json"])}
                          for w in db.all("SELECT * FROM watches ORDER BY name")]}

    def _watch(name: str):
        w = db.one("SELECT * FROM watches WHERE name=?", (name,))
        if w is None:
            raise HTTPException(404, "no existe")
        return w

    @api.get("/api/v1/watches/{name}/events", dependencies=[Depends(reader)])
    def watch_events(name: str, limit: int = Query(50, ge=1, le=200)):
        w = _watch(name)
        rows = db.all("""SELECT id, created_at, title, summary, claim_type, relevance, importance, cluster_id, digest_id,
                         alerted FROM events WHERE watch_id=? ORDER BY id DESC LIMIT ?""", (w["id"], limit))
        return {"items": [dict(r) for r in rows]}

    @api.get("/api/v1/watches/{name}/digests", dependencies=[Depends(reader)])
    def watch_digests(name: str):
        w = _watch(name)
        rows = db.all("SELECT id, created_at, period_start, period_end, summary, event_count FROM digests "
                      "WHERE watch_id=? ORDER BY id DESC", (w["id"],))
        return {"items": [dict(r) for r in rows]}

    @api.post("/api/v1/watches/{name}/{action}", dependencies=[Depends(writer)])
    def watch_toggle(name: str, action: str):
        if action not in ("enable", "disable"):
            raise HTTPException(404, "acción desconocida")
        from .monitor.service import WatchService
        _watch(name)
        WatchService(app).set_enabled(name, action == "enable")
        return {"name": name, "enabled": action == "enable"}

    @api.get("/api/v1/digests/{digest_id}", dependencies=[Depends(reader)])
    def digest(digest_id: int):
        d = db.one("SELECT markdown FROM digests WHERE id=?", (digest_id,))
        if d is None:
            raise HTTPException(404, "no existe")
        return PlainTextResponse(d["markdown"], media_type="text/markdown; charset=utf-8")

    @api.get("/api/v1/inbox", dependencies=[Depends(reader)])
    def inbox(unread: bool = True, limit: int = Query(50, ge=1, le=200)):
        rows = db.all(f"SELECT id, ts, kind, title, read, watch_id FROM notifications "
                      f"{'WHERE read=0' if unread else ''} ORDER BY id DESC LIMIT ?", (limit,))
        return {"items": [dict(r) for r in rows]}

    @api.get("/api/v1/search", dependencies=[Depends(reader)])
    def search(q: str = Query(..., min_length=2), limit: int = Query(20, ge=1, le=100)):
        return search_corpus(db, q, limit)

    @api.get("/api/v1/connectors", dependencies=[Depends(reader)])
    def connectors():
        out = []
        for cid, c in sorted(app.connectors.items()):
            st = c.status()
            cap = c.capabilities()
            out.append({"id": cid, "access_mode": cap.access_mode, "state": st.state, "detail": st.detail,
                        "operations": list(cap.operations), "notes": cap.notes})
        return {"items": out}

    # ------------------------------------------------------------------ dashboard (read-only)
    def page(title: str, body: str) -> HTMLResponse:
        return HTMLResponse(f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)} · ROS</title>
<style>
:root{{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#6b6a65;--line:#e4e1d8;--accent:#9b3d2e}}
@media (prefers-color-scheme: dark){{:root{{--bg:#151513;--fg:#ecebe6;--muted:#9c9a92;--line:#2f2e2a;--accent:#e08a6e}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,sans-serif;margin:0 auto;max-width:960px;padding:16px}}
a{{color:var(--accent)}} table{{border-collapse:collapse;width:100%}} td,th{{border-bottom:1px solid var(--line);
padding:6px 8px;text-align:left;vertical-align:top}} th{{color:var(--muted);font-weight:600}}
pre{{white-space:pre-wrap;background:rgba(127,127,127,.08);padding:12px;border-radius:6px}}
nav a{{margin-right:14px}} .muted{{color:var(--muted)}}
</style></head><body><nav><a href="/">Inicio</a><a href="/runs">Investigaciones</a><a href="/watches">Seguimientos</a>
<a href="/inbox">Bandeja</a></nav><h1>{html.escape(title)}</h1>{body}</body></html>""")

    e = html.escape

    @api.get("/", response_class=HTMLResponse, dependencies=[Depends(reader)])
    def home():
        h = health()
        notes = db.all("SELECT id, ts, kind, title FROM notifications WHERE read=0 ORDER BY id DESC LIMIT 10")
        body = (f"<p class=muted>Esquema v{h['schema_version']} · daemon: {'activo' if h['daemon'] else 'parado'} · "
                f"análisis pendientes: {h['pending_analysis']} · fuentes en pausa: {h['open_circuits']}</p>"
                "<h2>Sin leer</h2><table><tr><th>Fecha</th><th>Tipo</th><th>Título</th></tr>"
                + "".join(f"<tr><td>{e(n['ts'][:16])}</td><td>{e(n['kind'])}</td><td>{e(n['title'])}</td></tr>"
                          for n in notes) + "</table>")
        return page("ROS", body)

    @api.get("/runs", response_class=HTMLResponse, dependencies=[Depends(reader)])
    def runs_page():
        rows = db.all("SELECT id, kind, objective, status, created_at FROM runs WHERE kind='research' ORDER BY id DESC "
                      "LIMIT 100")
        return page("Investigaciones", "<table><tr><th>#</th><th>Objetivo</th><th>Estado</th><th>Fecha</th></tr>" + "".join(
            f"<tr><td><a href='/runs/{r['id']}'>{r['id']}</a></td><td>{e(r['objective'][:120])}</td>"
            f"<td>{e(r['status'])}</td><td>{e(r['created_at'][:16])}</td></tr>" for r in rows) + "</table>")

    @api.get("/runs/{run_id}", response_class=HTMLResponse, dependencies=[Depends(reader)])
    def run_page(run_id: int):
        r = _run(run_id)
        return page(f"Investigación #{run_id}", f"<p class=muted>{e(r['status'])}</p><pre>{e(r['report_md'] or '')}</pre>")

    @api.get("/watches", response_class=HTMLResponse, dependencies=[Depends(reader)])
    def watches_page():
        rows = db.all("SELECT * FROM watches ORDER BY name")
        return page("Seguimientos", "<table><tr><th>Nombre</th><th>Activo</th><th>Próxima ejecución</th>"
                    "<th>Próximo informe</th></tr>" + "".join(
                        f"<tr><td><a href='/watches/{e(w['name'])}'>{e(w['name'])}</a></td><td>{'sí' if w['enabled'] else 'no'}</td>"
                        f"<td>{e(w['next_run_at'] or '—')}</td><td>{e(w['next_digest_at'] or '—')}</td></tr>"
                        for w in rows) + "</table>")

    @api.get("/watches/{name}", response_class=HTMLResponse, dependencies=[Depends(reader)])
    def watch_page(name: str):
        w = _watch(name)
        evs = db.all("SELECT created_at, title, importance, claim_type FROM events WHERE watch_id=? ORDER BY id DESC "
                     "LIMIT 50", (w["id"],))
        digests = db.all("SELECT id, period_end, event_count FROM digests WHERE watch_id=? ORDER BY id DESC LIMIT 20",
                         (w["id"],))
        body = ("<h2>Informes</h2><ul>" + "".join(
            f"<li><a href='/digests/{d['id']}'>{e(d['period_end'][:16])}</a> ({d['event_count']} eventos)</li>"
            for d in digests) + "</ul><h2>Eventos recientes</h2><table><tr><th>Fecha</th><th>Evento</th><th>Tipo</th>"
                "<th>Importancia</th></tr>" + "".join(
            f"<tr><td>{e(v['created_at'][:16])}</td><td>{e(v['title'])}</td><td>{e(v['claim_type'])}</td>"
            f"<td>{v['importance']:.2f}</td></tr>" for v in evs) + "</table>")
        return page(f"Seguimiento: {name}", body)

    @api.get("/digests/{digest_id}", response_class=HTMLResponse, dependencies=[Depends(reader)])
    def digest_page(digest_id: int):
        d = db.one("SELECT markdown FROM digests WHERE id=?", (digest_id,))
        if d is None:
            raise HTTPException(404, "no existe")
        return page(f"Informe #{digest_id}", f"<pre>{e(d['markdown'])}</pre>")

    @api.get("/inbox", response_class=HTMLResponse, dependencies=[Depends(reader)])
    def inbox_page():
        rows = db.all("SELECT * FROM notifications ORDER BY id DESC LIMIT 100")
        return page("Bandeja", "".join(
            f"<details><summary>{e(n['ts'][:16])} · {e(n['kind'])} · {e(n['title'])}{'' if n['read'] else ' ●'}</summary>"
            f"<pre>{e(n['body'])}</pre></details>" for n in rows))

    return api
