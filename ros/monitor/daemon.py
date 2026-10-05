"""`ros daemon`: one persistent process that runs everything that is due.

Each tick (holding the `daemon` lock in SQLite, so only one daemon runs per database):
  1. expire watches past their duration;
  2. execute due watches (next_run_at <= now);
  3. finish analysis left pending by an interrupted execution (no re-download);
  4. generate due digests (time-based or by accumulated events);
  5. resume research runs that are queued or were interrupted by a crash;
  6. deliver pending notifications to external channels.

Shutdown (SIGTERM/SIGINT) stops taking new work, lets the current bounded step reach a checkpoint,
and releases the lock. Everything is restartable: state lives in SQLite, not in the process.
"""

from __future__ import annotations

import os
import signal
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable

from ..db import now_iso
from ..errors import RosError
from ..registry import App
from .digest import build_digest
from .schedule import Clock, SystemClock, iso, next_digest_at, parse_iso
from .service import WatchService

LOCK_TTL = 300
RECOVERY_PAUSE_MIN = 10


class Daemon:
    def __init__(self, app: App, *, clock: Clock | None = None, echo: Callable[[str], None] = print,
                 sleep=time.sleep):
        self.app = app
        self.db = app.db
        self.clock = clock or SystemClock()
        self.echo = echo
        self.sleep = sleep
        self.owner = f"daemon-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.stopping = False

    # ------------------------------------------------------------------ lifecycle
    def request_stop(self, *_args) -> None:
        if not self.stopping:
            self.echo("· apagando: se termina el paso actual y se libera el bloqueo…")
        self.stopping = True

    def acquire(self) -> bool:
        return self.db.acquire_lock("daemon", self.owner, LOCK_TTL)

    def release(self) -> None:
        self.db.release_lock("daemon", self.owner)

    def serve(self, poll_seconds: float = 30.0, max_ticks: int | None = None) -> None:
        if not self.acquire():
            raise RuntimeError("otro proceso `ros daemon` está activo sobre esta base de datos")
        try:
            previous = {s: signal.signal(s, self.request_stop) for s in (signal.SIGTERM, signal.SIGINT)}
        except ValueError:  # not in the main thread (tests)
            previous = {}
        ticks = 0
        try:
            while not self.stopping:
                self.tick()
                ticks += 1
                if max_ticks is not None and ticks >= max_ticks:
                    break
                waited = 0.0
                while waited < poll_seconds and not self.stopping:
                    self.sleep(min(1.0, poll_seconds - waited))
                    waited += 1.0
        finally:
            for s, handler in previous.items():
                signal.signal(s, handler)
            self.release()

    # ------------------------------------------------------------------ one pass
    def tick(self) -> dict:
        if not self.acquire():
            raise RuntimeError("otro proceso `ros daemon` está activo sobre esta base de datos")
        now = self.clock.now()
        done = {"watches": 0, "recovered": 0, "digests": 0, "research": 0, "delivered": 0, "expired": 0}
        service = WatchService(self.app, clock=self.clock, echo=self.echo, sleep=self.sleep)

        for w in self.db.all("SELECT * FROM watches WHERE enabled=1 AND expires_at IS NOT NULL"):
            if parse_iso(w["expires_at"]) <= now:
                self.db.execute("UPDATE watches SET enabled=0, updated_at=? WHERE id=?", (now_iso(), w["id"]))
                self.db.execute("""INSERT OR IGNORE INTO notifications (watch_id, ts, kind, title, body, dedupe_key)
                                   VALUES (?,?,?,?,?,?)""", (w["id"], now_iso(), "attention",
                                                             f"Seguimiento finalizado: {w['name']}",
                                                             f"El seguimiento terminó su duración ({w['expires_at']}).",
                                                             f"attention:{w['id']}:expired"))
                build_digest(self.app, w["id"], now=now, force=True, echo=self.echo)
                done["expired"] += 1

        ran: set[int] = set()
        for w in self.db.all("""SELECT id FROM watches WHERE enabled=1 AND (next_run_at IS NULL OR next_run_at <= ?)
                                ORDER BY next_run_at""", (iso(now),)):
            if self.stopping:
                return done
            self._guard(lambda: service.run_watch(w["id"], trigger="scheduler"), f"seguimiento {w['id']}")
            ran.add(w["id"])
            done["watches"] += 1

        # Recovery: analysis left pending by an interrupted execution. Rate-limited per watch so that a
        # backlog (or an exhausted budget) never turns into a hot loop of empty executions.
        quiet = iso(datetime.now(timezone.utc) - timedelta(minutes=RECOVERY_PAUSE_MIN))
        for w in self.db.all("""SELECT DISTINCT wi.watch_id FROM watch_items wi JOIN watches w ON w.id=wi.watch_id
                                WHERE wi.analysis='pending' AND w.enabled=1"""):
            if self.stopping:
                return done
            recent = self.db.one("SELECT MAX(created_at) t FROM runs WHERE watch_id=?", (w["watch_id"],))["t"]
            if w["watch_id"] in ran or (recent and recent > quiet):
                continue
            self._guard(lambda: service.analyze_pending(w["watch_id"]), f"recuperación {w['watch_id']}")
            done["recovered"] += 1

        for w in self.db.all("SELECT * FROM watches WHERE enabled=1"):
            if self.stopping:
                return done
            spec = service.spec_of(w)
            due = False
            if spec.digest.mode in ("daily", "weekly", "interval"):
                due = bool(w["next_digest_at"]) and parse_iso(w["next_digest_at"]) <= now
            elif spec.digest.mode == "threshold":
                pending = self.db.one("SELECT COUNT(*) c FROM events WHERE watch_id=? AND digest_id IS NULL",
                                      (w["id"],))["c"]
                due = pending >= spec.digest.min_events
            if not due:
                continue
            if self._guard(lambda: build_digest(self.app, w["id"], now=now, echo=self.echo), f"digest {w['name']}"):
                done["digests"] += 1
            nxt = next_digest_at(spec.digest, now, spec.timezone)
            self.db.execute("UPDATE watches SET next_digest_at=? WHERE id=?", (iso(nxt) if nxt else None, w["id"]))

        from ..research.engine import ResearchEngine, RunBusy
        for r in self.db.all("""SELECT id FROM runs WHERE kind='research' AND status IN ('queued','running')
                                ORDER BY id"""):
            if self.stopping:
                return done
            if not self.db.acquire_lock(f"run:{r['id']}", self.owner, 1):
                continue  # executing in another process (e.g. an interactive `ros research`)
            self.db.release_lock(f"run:{r['id']}", self.owner)
            engine = ResearchEngine(self.app, echo=self.echo, sleep=self.sleep, stop_requested=lambda: self.stopping)
            try:
                self.echo(f"▶ investigación #{r['id']}")
                engine.run(r["id"])
                done["research"] += 1
            except RunBusy:
                continue
            except RosError as exc:
                self.echo(f"✖ investigación #{r['id']}: {exc}")

        from ..notify import Notifier
        done["delivered"] = self._guard(lambda: Notifier(self.app, clock=self.clock).deliver_pending(),
                                        "notificaciones") or 0
        self.db.acquire_lock("daemon", self.owner, LOCK_TTL)
        return done

    def _guard(self, fn, what: str):
        """One failing watch/digest must not stop the daemon; the error is recorded and retried next tick."""
        try:
            return fn()
        except RosError as exc:
            self.echo(f"✖ {what}: {exc}")
            self.db.record_error(kind=exc.kind.value, subject=what, message=exc.message,
                                 impact="se reintentará en el próximo ciclo del daemon")
        except Exception as exc:  # noqa: BLE001 - keep the daemon alive, but never silently
            self.echo(f"✖ {what}: {type(exc).__name__}: {exc}")
            self.db.record_error(kind="internal", subject=what, message=f"{type(exc).__name__}: {exc}",
                                 impact="se reintentará en el próximo ciclo del daemon")
        return None
