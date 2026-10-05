"""Watches: storage, versioning and execution.

One execution of a watch (a `runs` row with kind='monitor'):

  0. analyse items left pending by an earlier interrupted execution
  1. for each source, outside any transaction: connector.sync(cursor)
     transaction A: items, snapshots, dedupe identities, pending analysis and the new cursor
  2. for each pending item, outside any transaction: model triage
     transaction B: event, evidence (claims), analysis marked done
  3. correlation of new events into clusters
  4. alert rules

If the process dies between A and B, nothing is downloaded again: the items are durable and
marked pending, and the next execution (or the daemon) analyses them.
"""

from __future__ import annotations

import difflib
import time
from datetime import datetime, timedelta
from typing import Callable

from ..budget import Budget, Ledger
from ..connectors.base import SourceSpec, SyncResult
from ..connectors.feeds import WebPageConnector, host_of
from ..db import dumps, loads, now_iso, now_precise, text_hash
from ..errors import BudgetExhausted, ErrorKind, RosError
from ..knowledge import feedback_adjustment, find_near_duplicate
from ..registry import App
from ..security import canonical_url, wrap_untrusted
from . import prompts
from .schedule import Clock, SystemClock, iso, next_digest_at, next_run_at, parse_iso
from .spec import WatchSpec

MAX_ANALYSIS_ATTEMPTS = 3
CIRCUIT_THRESHOLD = 3


class WatchService:
    def __init__(self, app: App, *, clock: Clock | None = None, echo: Callable[[str], None] = lambda s: None,
                 sleep=time.sleep):
        self.app = app
        self.db = app.db
        self.clock = clock or SystemClock()
        self.echo = echo
        self.sleep = sleep

    # ================================================================== storage
    def create(self, spec: WatchSpec, *, enabled: bool = False) -> int:
        if self.db.one("SELECT 1 FROM watches WHERE name=?", (spec.name,)):
            raise ValueError(f"ya existe un seguimiento llamado {spec.name!r}")
        ts = now_iso()
        with self.db.tx():
            wid = self.db.insert("watches", {"name": spec.name, "spec_json": spec.model_dump_json(), "version": 1,
                                             "enabled": 0, "expires_at": spec.expires_at, "created_at": ts,
                                             "updated_at": ts})
            self.db.insert("watch_versions", {"watch_id": wid, "version": 1, "spec_json": spec.model_dump_json(),
                                              "created_at": ts})
        if enabled:
            self.set_enabled(spec.name, True)
        return wid

    def get(self, name: str):
        row = self.db.one("SELECT * FROM watches WHERE name=?", (name.strip().lower(),))
        if row is None:
            raise ValueError(f"no existe el seguimiento {name!r} (ver `ros watch list`)")
        return row

    def by_id(self, watch_id: int):
        return self.db.one("SELECT * FROM watches WHERE id=?", (watch_id,))

    @staticmethod
    def spec_of(row) -> WatchSpec:
        return WatchSpec.model_validate_json(row["spec_json"])

    def update(self, name: str, spec: WatchSpec) -> int:
        """Store an edited spec as a new version. Returns the version number."""
        row = self.get(name)
        if spec.name != row["name"]:
            raise ValueError("el nombre de un seguimiento no se puede cambiar")
        if spec.model_dump_json() == row["spec_json"]:
            return row["version"]
        version = row["version"] + 1
        ts = now_iso()
        with self.db.tx():
            self.db.insert("watch_versions", {"watch_id": row["id"], "version": version,
                                              "spec_json": spec.model_dump_json(), "created_at": ts})
            self.db.execute("UPDATE watches SET spec_json=?, version=?, expires_at=?, updated_at=? WHERE id=?",
                            (spec.model_dump_json(), version, spec.expires_at, ts, row["id"]))
            if row["enabled"]:
                self._reschedule_digest(row["id"], spec)
        return version

    def set_enabled(self, name: str, enabled: bool) -> None:
        row = self.get(name)
        now = self.clock.now()
        if enabled:
            spec = self.spec_of(row)
            if spec.expires_at and parse_iso(spec.expires_at) and parse_iso(spec.expires_at) <= now:
                raise ValueError(f"el seguimiento {name!r} ya expiró ({spec.expires_at}); edita su duración")
            digest_at = next_digest_at(spec.digest, now, spec.timezone)
            self.db.execute("UPDATE watches SET enabled=1, next_run_at=COALESCE(next_run_at, ?), next_digest_at=?, "
                            "updated_at=? WHERE id=?",
                            (iso(now), iso(digest_at) if digest_at else None, now_iso(), row["id"]))
        else:
            self.db.execute("UPDATE watches SET enabled=0, updated_at=? WHERE id=?", (now_iso(), row["id"]))

    def _reschedule_digest(self, watch_id: int, spec: WatchSpec) -> None:
        digest_at = next_digest_at(spec.digest, self.clock.now(), spec.timezone)
        self.db.execute("UPDATE watches SET next_digest_at=? WHERE id=?", (iso(digest_at) if digest_at else None, watch_id))

    def delete(self, name: str) -> None:
        """Remove the watch and its monitor memory. Executions stay in the history, detached."""
        row = self.get(name)
        with self.db.tx():
            self.db.execute("UPDATE runs SET watch_id=NULL WHERE watch_id=?", (row["id"],))
            self.db.execute("DELETE FROM watches WHERE id=?", (row["id"],))

    def list(self):
        return self.db.all("SELECT * FROM watches ORDER BY name")

    def versions(self, name: str):
        row = self.get(name)
        return self.db.all("SELECT * FROM watch_versions WHERE watch_id=? ORDER BY version", (row["id"],))

    # ================================================================== validation
    def check_sources(self, spec: WatchSpec, *, probe: bool = True) -> list[tuple[str, str, str]]:
        """[(source key, 'ok'|'error', detail)]. Without probe only checks the connector exists and is usable."""
        out = []
        for ref in spec.sources:
            try:
                connector = self.app.connector(ref.kind)
                if probe:
                    resolved = connector.validate(ref.locator)
                    out.append((ref.key(), "ok", resolved.label or resolved.locator))
                else:
                    out.append((ref.key(), "ok", connector.capabilities().notes))
            except RosError as exc:
                out.append((ref.key(), "error", str(exc)))
        return out

    # ================================================================== execution
    def run_watch(self, watch_id: int, *, trigger: str = "user") -> dict:
        row = self.by_id(watch_id)
        spec = self.spec_of(row)
        now = self.clock.now()
        allowance = self.daily_allowance()
        budget = Budget(max_rounds=1, max_sources=10_000, max_minutes=30, max_tokens=2_000_000,
                        max_cost_usd=max(0.0, min(spec.max_cost_usd_per_run, allowance)), final_reserve=0.0,
                        retries=self.app.settings.budget.retries)
        ts = now_iso()
        run_id = self.db.insert("runs", {"kind": "monitor", "objective": spec.objective, "status": "running",
                                         "stage": "collecting", "watch_id": watch_id, "trigger": trigger,
                                         "config_json": dumps({"watch_version": row["version"]}),
                                         "budget_json": dumps(budget.to_dict()), "created_at": ts, "updated_at": ts})
        ledger = Ledger(self.db, run_id, budget)
        summary = {"run_id": run_id, "sources_ok": 0, "sources_failed": 0, "sources_skipped": 0, "new": 0,
                   "modified": 0, "baseline": 0, "duplicates": 0, "analyzed": 0, "events": 0, "alerts": 0,
                   "pending": 0, "stopped": None}
        self.echo(f"▶ {spec.name} (ejecución #{run_id})")
        try:
            if allowance > 0:
                self._analyze_pending(watch_id, spec, run_id, ledger, summary)
            for ref in spec.sources:
                self._collect(watch_id, spec, ref, run_id, ledger, summary, now)
            self._set_run(run_id, stage="analyzing")
            if allowance > 0:
                self._analyze_pending(watch_id, spec, run_id, ledger, summary)
            else:
                summary["stopped"] = "límite diario de coste de seguimientos alcanzado"
                self._attention(watch_id, f"limit:{now.date()}", "Límite diario de coste alcanzado",
                                "Los seguimientos siguen recopilando, pero el análisis queda pendiente hasta mañana "
                                f"(monitor_daily_cost_usd = ${self.app.settings.monitor_daily_cost_usd:.2f}).")
            self._link_duplicates(watch_id)
            from .correlate import correlate
            self._set_run(run_id, stage="correlating")
            correlate(self.app, watch_id, spec, ledger, run_id, now=now, echo=self.echo)
            from .digest import evaluate_alerts
            summary["alerts"] = len(evaluate_alerts(self.db, watch_id, spec, now=now))
        except BudgetExhausted as exc:
            summary["stopped"] = exc.message
        except RosError as exc:
            self.db.record_error(run_id=run_id, watch_id=watch_id, kind=exc.kind.value, message=exc.message,
                                 impact="ejecución del seguimiento detenida; se reintentará")
            summary["stopped"] = str(exc)
            if exc.fatal:
                self._attention(watch_id, f"fatal:{exc.kind.value}:{now.date()}", "ROS necesita tu intervención",
                                f"El seguimiento {spec.name} se detuvo: {exc}")
        summary["pending"] = self.db.one("SELECT COUNT(*) c FROM watch_items WHERE watch_id=? AND analysis='pending'",
                                         (watch_id,))["c"]
        status = "completed"
        if summary["sources_failed"] or summary["stopped"] or summary["pending"]:
            status = "partial"
        if summary["sources_failed"] and not summary["sources_ok"] and not summary["sources_skipped"]:
            status = "failed"
        ledger.checkpoint_time()
        note = self._coverage_note(summary)
        self._set_run(run_id, status=status, stage="done", outcome_note=note, finished_at=now_iso(),
                      report_md=dumps(summary))
        self.db.execute("UPDATE watches SET last_run_at=?, next_run_at=?, updated_at=? WHERE id=?",
                        (iso(now), iso(next_run_at(spec, now)), now_iso(), watch_id))
        self.echo(f"  {note}")
        summary["status"] = status
        return summary

    @staticmethod
    def _coverage_note(s: dict) -> str:
        parts = [f"{s['new']} nuevos", f"{s['modified']} modificados", f"{s['events']} eventos relevantes",
                 f"{s['alerts']} alertas", f"fuentes ok {s['sources_ok']}"]
        if s["sources_failed"]:
            parts.append(f"fallidas {s['sources_failed']}")
        if s["sources_skipped"]:
            parts.append(f"en pausa {s['sources_skipped']}")
        if s["baseline"]:
            parts.append(f"{s['baseline']} en línea base")
        if s["duplicates"]:
            parts.append(f"{s['duplicates']} duplicados")
        if s["pending"]:
            parts.append(f"{s['pending']} pendientes de análisis")
        if s["stopped"]:
            parts.append(f"detenido: {s['stopped']}")
        return " · ".join(parts)

    def _set_run(self, run_id: int, **values) -> None:
        values["updated_at"] = now_iso()
        cols = ", ".join(f"{k}=?" for k in values)
        self.db.execute(f"UPDATE runs SET {cols} WHERE id=?", (*values.values(), run_id))

    def daily_allowance(self) -> float:
        """USD the monitoring side may still spend today (UTC day), across all watches and digests."""
        day = self.clock.now().date().isoformat()
        spent = self.db.one("""SELECT COALESCE(SUM(u.cost_usd),0) c FROM usage u JOIN runs r ON r.id=u.run_id
                               WHERE r.kind IN ('monitor','digest') AND substr(u.ts,1,10)=?""", (day,))["c"]
        return max(0.0, self.app.settings.monitor_daily_cost_usd - spent)

    # ------------------------------------------------------------------ collect (transaction A)
    def _collect(self, watch_id: int, spec: WatchSpec, ref, run_id: int, ledger: Ledger, summary: dict,
                 now: datetime) -> None:
        label = ref.label or ref.locator
        try:
            connector = self.app.connector(ref.kind)
        except RosError as exc:
            summary["sources_failed"] += 1
            self.db.record_error(run_id=run_id, watch_id=watch_id, kind=exc.kind.value, subject=ref.key(),
                                 message=exc.message, impact="fuente no consultada")
            self._attention(watch_id, f"source:{ref.key()}:{now.date()}", f"Fuente no disponible: {label}", str(exc))
            return
        source_key_row = self.db.one("SELECT id FROM sources WHERE kind=? AND locator=?", (ref.kind, ref.locator))
        source_id = source_key_row["id"] if source_key_row else self.db.upsert_source(
            ref.kind, ref.locator, getattr(connector, "access_mode", "public_web"), ref.label or None)
        open_until = self._circuit_open(source_id, now)
        if open_until:
            summary["sources_skipped"] += 1
            self.echo(f"  ⏸ {label}: en pausa hasta {open_until} tras fallos repetidos")
            return
        cursor_row = self.db.one("SELECT cursor_json FROM cursors WHERE watch_id=? AND source_id=?", (watch_id, source_id))
        first_sync = cursor_row is None
        cursor = loads(cursor_row["cursor_json"], {}) if cursor_row else {}
        result = self._sync_with_retries(connector, ref, cursor, ledger)
        if result.error:
            summary["sources_failed"] += 1
            err = result.error
            self.db.record_error(run_id=run_id, watch_id=watch_id, kind=err.kind.value, subject=ref.key(),
                                 message=err.message, impact="sin datos nuevos de esta fuente en esta ejecución")
            failures = self._record_failure(source_id, err, now, spec.every_minutes)
            self.echo(f"  ✖ {label}: {err.kind.value}")
            if err.kind in (ErrorKind.INVALID_CREDENTIALS, ErrorKind.PLATFORM_BLOCKED) or failures == CIRCUIT_THRESHOLD:
                self._attention(watch_id, f"source:{ref.key()}:{err.kind.value}:{now.date()}",
                                f"Problema con la fuente {label}",
                                f"{err} — fallos consecutivos: {failures}.")
            if not result.items:
                return
        else:
            self._record_ok(source_id, now)
            summary["sources_ok"] += 1
        ledger.record("fetch", purpose=f"sync:{ref.kind}", bytes=result.bytes)
        for w in result.warnings:
            self.db.log(run_id, "warning", f"{label}: {w}")
        counts = self._store(watch_id, spec, source_id, result, run_id, first_sync)
        for k, v in counts.items():
            summary[k] += v
        self.echo(f"  ✔ {label}: {counts['new']} nuevos, {counts['modified']} modificados")

    def _sync_with_retries(self, connector, ref, cursor: dict, ledger: Ledger) -> SyncResult:
        attempt = 0
        while True:
            try:
                locator = cursor.get("_locator")
                title = cursor.get("_label", ref.label)
                if not locator:
                    resolved = connector.validate(ref.locator)
                    locator, title = resolved.locator, ref.label or resolved.label
                result = connector.sync(SourceSpec(ref.kind, locator, title), cursor)
                if result.error is None or not result.error.retryable or attempt >= ledger.budget.retries:
                    result.cursor = {**result.cursor, "_locator": locator, "_label": title}
                    return result
                wait = result.error.retry_after
            except RosError as exc:
                if not exc.retryable or attempt >= ledger.budget.retries:
                    return SyncResult([], cursor, complete=False, error=exc)
                wait = exc.retry_after
            attempt += 1
            self.sleep(min(wait or 2 ** attempt, 30))

    def _store(self, watch_id: int, spec: WatchSpec, source_id: int, result: SyncResult, run_id: int,
               first_sync: bool) -> dict:
        counts = {"new": 0, "modified": 0, "baseline": 0, "duplicates": 0}
        ts = now_iso()
        observed = now_precise()
        # Newest first so that the baseline keeps the most recent items for analysis.
        items = sorted(result.items, key=lambda i: i.published_at or "", reverse=True)
        with self.db.tx():
            for rank, it in enumerate(items):
                body = f"{it.title}\n{it.text}"
                h = text_hash(body)
                cid = canonical_url(it.url) if it.url else None
                existing = self.db.one("SELECT id, content_hash FROM items WHERE source_id=? AND external_id=?",
                                       (source_id, it.external_id))
                if existing:
                    item_id = existing["id"]
                    seen_before = self.db.one("SELECT 1 FROM watch_items WHERE watch_id=? AND item_id=?",
                                              (watch_id, item_id))
                    if existing["content_hash"] == h:
                        self.db.execute("UPDATE items SET last_seen_at=? WHERE id=?", (ts, item_id))
                        if seen_before:
                            continue
                        change = "new"
                    else:
                        self.db.execute("UPDATE items SET title=?, text=?, content_hash=?, last_seen_at=?, url=COALESCE(?, url) "
                                        "WHERE id=?", (it.title, it.text, h, ts, it.url, item_id))
                        change = "modified" if seen_before else "new"
                    dup_of = self.db.one("SELECT duplicate_of d FROM items WHERE id=?", (item_id,))["d"]
                else:
                    dup = self.db.one("""SELECT id FROM items WHERE duplicate_of IS NULL AND source_id<>? AND
                                         (content_hash=? OR (canonical_url IS NOT NULL AND canonical_url=?))
                                         ORDER BY id LIMIT 1""", (source_id, h, cid))
                    dup_of = dup["id"] if dup else None
                    item_id = self.db.insert("items", {
                        "source_id": source_id, "external_id": it.external_id, "url": it.url, "canonical_url": cid,
                        "title": it.title, "author": it.author, "published_at": it.published_at, "first_seen_at": ts,
                        "last_seen_at": ts, "content_hash": h, "text": it.text, "meta_json": dumps(it.meta),
                        "duplicate_of": dup_of})
                    change = "new"
                self.db.execute("INSERT OR IGNORE INTO snapshots (item_id, content_hash, observed_at, text) VALUES (?,?,?,?)",
                                (item_id, h, ts, it.text))
                analysis, reason = "pending", None
                if change == "new" and first_sync and rank >= spec.backfill:
                    analysis, reason = "skipped", "línea base (primera sincronización)"
                    counts["baseline"] += 1
                elif dup_of and self._observed(watch_id, dup_of):
                    # Only a copy of something THIS watch already has is skipped (and later linked to its event).
                    analysis, reason = "skipped", f"duplicado del item {dup_of}"
                    counts["duplicates"] += 1
                elif change == "new":
                    near = find_near_duplicate(self.db, it.text, exclude_item_id=item_id, days=7, limit=200)
                    if near:
                        self.db.execute("UPDATE items SET near_duplicate_of=COALESCE(near_duplicate_of, ?) WHERE id=?",
                                        (near, item_id))
                        if self._observed(watch_id, near):
                            analysis, reason = "skipped", f"casi duplicado del item {near}"
                            counts["duplicates"] += 1
                if analysis == "pending":
                    counts[change] += 1
                self.db.execute("""INSERT OR IGNORE INTO watch_items (watch_id, item_id, run_id, change, analysis,
                                   observed_at, source_id, reason) VALUES (?,?,?,?,?,?,?,?)""",
                                (watch_id, item_id, run_id, change, analysis, observed, source_id, reason))
            self.db.execute("INSERT OR REPLACE INTO cursors (watch_id, source_id, cursor_json, updated_at) VALUES (?,?,?,?)",
                            (watch_id, source_id, dumps(result.cursor), ts))
        return counts

    def _observed(self, watch_id: int, item_id: int) -> bool:
        return bool(self.db.one("SELECT 1 FROM watch_items WHERE watch_id=? AND item_id=?", (watch_id, item_id)))

    # ------------------------------------------------------------------ circuit breaker
    def _circuit_open(self, source_id: int, now: datetime) -> str | None:
        row = self.db.one("SELECT open_until FROM source_health WHERE source_id=?", (source_id,))
        if row and row["open_until"] and parse_iso(row["open_until"]) > now:
            return row["open_until"]
        return None

    def _record_failure(self, source_id: int, err: RosError, now: datetime, every_minutes: int) -> int:
        row = self.db.one("SELECT failures FROM source_health WHERE source_id=?", (source_id,))
        failures = (row["failures"] if row else 0) + 1
        open_until = None
        if err.retry_after:
            open_until = now + timedelta(seconds=err.retry_after)
        elif failures >= CIRCUIT_THRESHOLD:
            open_until = now + timedelta(minutes=min(every_minutes * 2 ** (failures - CIRCUIT_THRESHOLD), 1440))
        self.db.execute("""INSERT INTO source_health (source_id, failures, open_until, last_error, updated_at)
                           VALUES (?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET failures=excluded.failures,
                           open_until=excluded.open_until, last_error=excluded.last_error, updated_at=excluded.updated_at""",
                        (source_id, failures, iso(open_until) if open_until else None, str(err)[:500], now_iso()))
        return failures

    def _record_ok(self, source_id: int, now: datetime) -> None:
        self.db.execute("""INSERT INTO source_health (source_id, failures, open_until, last_ok_at, updated_at)
                           VALUES (?,0,NULL,?,?) ON CONFLICT(source_id) DO UPDATE SET failures=0, open_until=NULL,
                           last_ok_at=excluded.last_ok_at, updated_at=excluded.updated_at""",
                        (source_id, iso(now), now_iso()))

    # ------------------------------------------------------------------ analyse (transaction B)
    def analyze_pending(self, watch_id: int, *, trigger: str = "recovery") -> dict:
        """Recovery entry point: analyse what an interrupted execution left pending, without collecting."""
        row = self.by_id(watch_id)
        spec = self.spec_of(row)
        budget = Budget(max_rounds=1, max_minutes=30, max_tokens=2_000_000, final_reserve=0.0,
                        max_cost_usd=max(0.0, min(spec.max_cost_usd_per_run, self.daily_allowance())))
        if budget.max_cost_usd <= 0:
            return {"analyzed": 0, "events": 0, "stopped": "límite diario de coste alcanzado"}
        ts = now_iso()
        run_id = self.db.insert("runs", {"kind": "monitor", "objective": spec.objective, "status": "running",
                                         "stage": "analyzing", "watch_id": watch_id, "trigger": trigger,
                                         "budget_json": dumps(budget.to_dict()), "created_at": ts, "updated_at": ts})
        ledger = Ledger(self.db, run_id, budget)
        summary = {"analyzed": 0, "events": 0, "stopped": None}
        try:
            self._analyze_pending(watch_id, spec, run_id, ledger, summary)
            self._link_duplicates(watch_id)
            from .correlate import correlate
            from .digest import evaluate_alerts
            correlate(self.app, watch_id, spec, ledger, run_id, now=self.clock.now(), echo=self.echo)
            summary["alerts"] = len(evaluate_alerts(self.db, watch_id, spec, now=self.clock.now()))
        except BudgetExhausted as exc:
            summary["stopped"] = exc.message
        pending = self.db.one("SELECT COUNT(*) c FROM watch_items WHERE watch_id=? AND analysis='pending'", (watch_id,))["c"]
        ledger.checkpoint_time()
        self._set_run(run_id, status="partial" if pending else "completed", stage="done", finished_at=now_iso(),
                      outcome_note=f"{summary['analyzed']} analizados, {summary['events']} eventos, {pending} pendientes",
                      report_md=dumps(summary))
        return summary

    def _analyze_pending(self, watch_id: int, spec: WatchSpec, run_id: int, ledger: Ledger, summary: dict) -> None:
        rows = self.db.all(
            """SELECT wi.item_id, wi.observed_at, wi.change, wi.source_id, wi.attempts, i.title, i.url, i.text,
                      i.published_at, i.author, s.kind skind, s.locator slocator, s.title stitle
               FROM watch_items wi JOIN items i ON i.id=wi.item_id LEFT JOIN sources s ON s.id=wi.source_id
               WHERE wi.watch_id=? AND wi.analysis='pending' ORDER BY wi.observed_at, wi.item_id LIMIT ?""",
            (watch_id, spec.max_items_per_run))
        for r in rows:
            if self._excluded(spec, r["title"] or ""):
                self._mark(watch_id, r, "done", "excluido por las reglas del seguimiento")
                continue
            text = r["text"] or ""
            if spec.depth == "deep" and r["url"] and len(text) < 600:
                text = self._deepen(r, text, run_id, ledger)
            try:
                data = self._triage(spec, r, text, ledger)
            except BudgetExhausted:
                raise
            except RosError as exc:
                if exc.fatal:
                    raise
                attempts = r["attempts"] + 1
                self.db.record_error(run_id=run_id, watch_id=watch_id, kind=exc.kind.value, subject=r["url"] or r["title"],
                                     message=exc.message, impact="item pendiente de análisis" if attempts <
                                     MAX_ANALYSIS_ATTEMPTS else "item descartado tras varios intentos")
                if attempts >= MAX_ANALYSIS_ATTEMPTS:
                    self._mark(watch_id, r, "skipped", f"análisis fallido: {exc.kind.value}")
                else:
                    self.db.execute("UPDATE watch_items SET attempts=? WHERE watch_id=? AND item_id=? AND observed_at=?",
                                    (attempts, watch_id, r["item_id"], r["observed_at"]))
                continue
            summary["analyzed"] += 1
            if self._store_event(watch_id, spec, r, data, run_id):
                summary["events"] += 1

    @staticmethod
    def _excluded(spec: WatchSpec, title: str) -> bool:
        low = title.lower()
        return any(x.strip() and x.strip().lower() in low for x in spec.exclusions)

    def _deepen(self, r, text: str, run_id: int, ledger: Ledger) -> str:
        """Depth 'deep': fetch the linked page when the feed only carries a teaser."""
        try:
            doc = WebPageConnector(self.app.fetcher).fetch(r["url"])
        except RosError as exc:
            self.db.log(run_id, "deepen_failed", f"{r['url']}: {exc}")
            return text
        ledger.record("fetch", purpose="deepen", bytes=doc.meta.get("bytes", 0))
        self.db.execute("UPDATE items SET text=? WHERE id=?", (doc.text, r["item_id"]))
        return doc.text

    def _triage(self, spec: WatchSpec, r, text: str, ledger: Ledger) -> dict:
        previous = ""
        if r["change"] == "modified":
            prev = self.db.one("""SELECT text FROM snapshots WHERE item_id=? AND observed_at < ? AND text IS NOT NULL
                                  ORDER BY observed_at DESC LIMIT 1""", (r["item_id"], r["observed_at"]))
            if prev:
                diff = difflib.unified_diff(prev["text"].splitlines(), text.splitlines(), lineterm="", n=0)
                previous = "\n".join(list(diff)[2:200])
        source = r["stitle"] or r["slocator"] or ""
        body = (f"SOURCE: {r['skind']} {source}\nTITLE: {r['title']}\nURL: {r['url'] or ''}\n"
                f"PUBLISHED: {r['published_at'] or 'unknown'}\nAUTHOR: {r['author'] or 'unknown'}\n"
                f"CHANGE: {r['change']}\n\n{text}")
        if previous:
            body += f"\n\nCHANGES SINCE THE PREVIOUS VERSION (unified diff):\n{previous}"
        wrapped, _ = wrap_untrusted(body, label=r["url"] or source, max_chars=self.app.settings.doc_chars)
        user = (f"Watch interest:\n{spec.objective}\n\nTopics to detect: {', '.join(spec.topics) or '(any)'}\n"
                f"Exclude: {', '.join(spec.exclusions) or '(nothing)'}\n\nItem:\n{wrapped}")
        llm = self.app.llm(prompts.STAGE_ROLE["triage"])
        return llm.complete_json(purpose="triage", system=prompts.TRIAGE_SYSTEM, user=user,
                                 schema=prompts.TRIAGE_SCHEMA, max_tokens=prompts.MAX_TOKENS["triage"], ledger=ledger)

    def _mark(self, watch_id: int, r, analysis: str, reason: str | None) -> None:
        self.db.execute("UPDATE watch_items SET analysis=?, reason=? WHERE watch_id=? AND item_id=? AND observed_at=?",
                        (analysis, reason, watch_id, r["item_id"], r["observed_at"]))

    def _store_event(self, watch_id: int, spec: WatchSpec, r, data: dict, run_id: int) -> bool:
        relevance = max(0.0, min(1.0, float(data["relevance"])))
        meta_patch = {"injection_suspected": data["injection_suspected"]}
        if data["injection_suspected"]:
            self.db.log(run_id, "injection", f"posible prompt injection en {r['url'] or r['title']}; tratado como dato")
        with self.db.tx():
            meta = loads(self.db.one("SELECT meta_json FROM items WHERE id=?", (r["item_id"],))["meta_json"], {}) or {}
            self.db.execute("UPDATE items SET meta_json=? WHERE id=?", (dumps({**meta, **meta_patch}), r["item_id"]))
            if not data["relevant"] or relevance < spec.min_relevance:
                self._mark(watch_id, r, "done", f"no relevante ({relevance:.2f})")
                return False
            host = host_of(r["url"]) or (r["slocator"] or "")
            factor, reasons = feedback_adjustment(self.db, watch_id, data["labels"], data["entities"], host)
            importance = max(0.0, min(1.0, float(data["importance"]) * factor))
            policy = {"model_importance": data["importance"], "feedback_factor": round(factor, 3),
                      "feedback": reasons} if reasons else None
            cur = self.db.execute(
                """INSERT OR IGNORE INTO events (watch_id, run_id, item_id, created_at, title, summary, change, relevance,
                   importance, claim_type, labels_json, why, source_id, observed_at, entities_json, policy_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (watch_id, run_id, r["item_id"], now_iso(), data["title"].strip()[:300] or (r["title"] or "")[:300],
                 data["summary"].strip()[:2000], r["change"], relevance, importance, data["claim_type"],
                 dumps(data["labels"][:12]), data["why"][:500], r["source_id"], r["observed_at"],
                 dumps(data["entities"][:20]), dumps(policy) if policy else None))
            created = bool(cur.rowcount)
            if created:
                event_id = cur.lastrowid
                self.db.execute("INSERT OR IGNORE INTO event_items (event_id, item_id, relation) VALUES (?,?,'primary')",
                                (event_id, r["item_id"]))
                for c in data["claims"][:5]:
                    if c["text"].strip():
                        self.db.insert("claims", {"run_id": run_id, "item_id": r["item_id"], "text": c["text"].strip()[:1000],
                                                  "claim_type": c["type"], "quote": c["quote"][:400],
                                                  "confidence": max(0.0, min(1.0, float(c["confidence"]))),
                                                  "created_at": now_iso()})
            self._mark(watch_id, r, "done", None)
        if created:
            self.echo(f"  ● {data['title'][:90]} (importancia {importance:.2f})")
        return created

    def _link_duplicates(self, watch_id: int) -> None:
        """Copies of an analysed item (other sources) become extra sources of its event: corroboration."""
        self.db.execute(
            """INSERT OR IGNORE INTO event_items (event_id, item_id, relation)
               SELECT e.id, i.id, CASE WHEN i.duplicate_of IS NOT NULL THEN 'duplicate' ELSE 'near_duplicate' END
               FROM watch_items wi JOIN items i ON i.id=wi.item_id
               JOIN events e ON e.watch_id=wi.watch_id AND e.item_id=COALESCE(i.duplicate_of, i.near_duplicate_of)
               WHERE wi.watch_id=? AND (i.duplicate_of IS NOT NULL OR i.near_duplicate_of IS NOT NULL)""", (watch_id,))

    # ------------------------------------------------------------------ notifications
    def _attention(self, watch_id: int | None, key: str, title: str, body: str) -> None:
        self.db.execute("""INSERT OR IGNORE INTO notifications (watch_id, ts, kind, title, body, dedupe_key)
                           VALUES (?,?,?,?,?,?)""", (watch_id, now_iso(), "attention", title, body,
                                                    f"attention:{watch_id}:{key}"))
