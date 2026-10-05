"""Delivery of notifications (alerts, digests, attention requests) outside the local inbox.

The `notifications` table is the durable intent; `deliveries` records one row per
(notification, channel), so a notification is sent at most once per channel even if the daemon
restarts mid-delivery. Webhooks carry an Idempotency-Key for receivers that deduplicate.
Failed deliveries retry with exponential backoff and stop after MAX_ATTEMPTS ('dead').
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Callable

import httpx

from .db import now_iso
from .errors import ErrorKind, RosError
from .monitor.schedule import Clock, SystemClock, iso, parse_iso
from .registry import App

MAX_ATTEMPTS = 5
TELEGRAM_LIMIT = 4000


class Notifier:
    def __init__(self, app: App, *, clock: Clock | None = None, client: httpx.Client | None = None,
                 smtp_factory: Callable[..., smtplib.SMTP] = smtplib.SMTP, env: dict | None = None):
        self.app = app
        self.db = app.db
        self.s = app.settings
        self.clock = clock or SystemClock()
        self.client = client or httpx.Client(timeout=20.0)
        self.smtp_factory = smtp_factory
        self.env = env if env is not None else os.environ

    def channels(self) -> dict[str, Callable[[dict], None]]:
        out = {}
        if self.s.notify_webhook_url:
            out["webhook"] = self._webhook
        if self.s.notify_telegram_chat_id and self.env.get(self.s.notify_telegram_token_env):
            out["telegram"] = self._telegram
        if self.s.notify_email_to and self.s.notify_smtp_host:
            out["email"] = self._email
        return out

    def deliver_pending(self) -> int:
        channels = self.channels()
        if not channels:
            return 0
        kinds = tuple(self.s.notify_kinds)
        now = self.clock.now()
        # Only recent notifications: enabling a channel must not replay the whole history.
        since = iso(datetime.now(timezone.utc) - timedelta(hours=48))   # notification ts is wall-clock
        with self.db.tx():
            for name in channels:
                self.db.execute(
                    f"""INSERT OR IGNORE INTO deliveries (notification_id, channel, status)
                        SELECT id, ?, 'pending' FROM notifications
                        WHERE kind IN ({','.join('?' * len(kinds))}) AND ts >= ?""",
                    (name, *kinds, since))
        sent = 0
        rows = self.db.all("""SELECT d.id did, d.channel, d.attempts, d.next_attempt_at, n.* FROM deliveries d
                              JOIN notifications n ON n.id=d.notification_id WHERE d.status='pending'
                              ORDER BY n.id""")
        for r in rows:
            if r["channel"] not in channels:
                continue
            if r["next_attempt_at"] and parse_iso(r["next_attempt_at"]) > now:
                continue
            note = {"id": r["id"], "kind": r["kind"], "title": r["title"], "body": r["body"],
                    "dedupe_key": r["dedupe_key"], "ts": r["ts"], "watch_id": r["watch_id"]}
            try:
                channels[r["channel"]](note)
            except (RosError, OSError, smtplib.SMTPException, httpx.HTTPError) as exc:
                attempts = r["attempts"] + 1
                dead = attempts >= MAX_ATTEMPTS
                retry = now + timedelta(minutes=2 ** attempts)
                self.db.execute("UPDATE deliveries SET attempts=?, status=?, next_attempt_at=?, last_error=? WHERE id=?",
                                (attempts, "dead" if dead else "pending", iso(retry), str(exc)[:500], r["did"]))
                if dead:
                    self.db.record_error(watch_id=r["watch_id"], kind="internal", subject=f"entrega {r['channel']}",
                                         message=str(exc), impact=f"notificación {r['id']} no entregada por {r['channel']}")
                continue
            self.db.execute("UPDATE deliveries SET status='sent', attempts=attempts+1, delivered_at=? WHERE id=?",
                            (now_iso(), r["did"]))
            sent += 1
        return sent

    # ------------------------------------------------------------------ channels
    def _webhook(self, note: dict) -> None:
        payload = json.dumps(note, ensure_ascii=False).encode()
        headers = {"Content-Type": "application/json", "Idempotency-Key": note["dedupe_key"],
                   "User-Agent": "ROS-notify/0.2"}
        secret = self.env.get(self.s.notify_webhook_secret_env)
        if secret:
            headers["X-ROS-Signature"] = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        resp = self.client.post(self.s.notify_webhook_url, content=payload, headers=headers)
        if resp.status_code >= 300:
            raise RosError(ErrorKind.SOURCE_UNREACHABLE, f"webhook respondió HTTP {resp.status_code}")

    def _telegram(self, note: dict) -> None:
        token = self.env.get(self.s.notify_telegram_token_env, "")
        text = f"{note['title']}\n\n{note['body']}"
        if len(text) > TELEGRAM_LIMIT:
            text = text[: TELEGRAM_LIMIT - 40] + "\n\n… (completo en `ros inbox`)"
        resp = self.client.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                json={"chat_id": self.s.notify_telegram_chat_id, "text": text,
                                      "disable_web_page_preview": True})
        if resp.status_code >= 300:
            raise RosError(ErrorKind.SOURCE_UNREACHABLE, f"Telegram respondió HTTP {resp.status_code}")

    def _email(self, note: dict) -> None:
        msg = EmailMessage()
        msg["Subject"] = note["title"]
        msg["From"] = self.s.notify_email_from or self.s.notify_email_to
        msg["To"] = self.s.notify_email_to
        msg["Message-ID"] = f"<{note['dedupe_key'].replace(':', '.')}@ros.local>"
        msg.set_content(note["body"])
        with self.smtp_factory(self.s.notify_smtp_host, self.s.notify_smtp_port, timeout=30) as smtp:
            smtp.starttls()
            if self.s.notify_smtp_user:
                smtp.login(self.s.notify_smtp_user, self.env.get(self.s.notify_smtp_password_env, ""))
            smtp.send_message(msg)
