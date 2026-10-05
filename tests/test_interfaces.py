"""Delivery channels, local API/dashboard and Obsidian export."""

import json
import os
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from ros.api import create_app, load_or_create_token
from ros.connectors.search import StaticSearchBackend
from ros.db import now_iso
from ros.notify import Notifier
from ros.obsidian import export_vault, safe_path
from ros.offline import offline_handler


@pytest.fixture
def app(make_app):
    return make_app(offline_handler, StaticSearchBackend(lambda q, n: []))


def _notification(db, key="alert:event:1", kind="alert"):
    return db.insert("notifications", {"ts": now_iso(), "kind": kind, "title": "⚠ algo", "body": "cuerpo",
                                       "dedupe_key": key})


# ---------------------------------------------------------------- notifications
def test_webhook_delivery_is_idempotent_and_signed(app, db):
    sent = []
    client = httpx.Client(transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200)))
    app.settings = replace(app.settings, notify_webhook_url="https://hooks.example/ros")
    _notification(db)
    n = Notifier(app, client=client, env={"ROS_WEBHOOK_SECRET": "s3cret"})
    assert n.deliver_pending() == 1
    assert n.deliver_pending() == 0                       # never sent twice
    assert len(sent) == 1
    req = sent[0]
    assert req.headers["idempotency-key"] == "alert:event:1"
    assert req.headers["x-ros-signature"].startswith("sha256=")
    assert json.loads(req.content)["title"] == "⚠ algo"


def test_failed_delivery_backs_off_and_eventually_gives_up(app, db):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    app.settings = replace(app.settings, notify_webhook_url="https://hooks.example/ros")
    _notification(db)
    n = Notifier(app, client=client, env={})
    assert n.deliver_pending() == 0
    row = db.one("SELECT * FROM deliveries")
    assert row["status"] == "pending" and row["attempts"] == 1 and row["next_attempt_at"]
    for _ in range(6):
        db.execute("UPDATE deliveries SET next_attempt_at=NULL")
        n.deliver_pending()
    assert db.one("SELECT status FROM deliveries")["status"] == "dead"
    assert db.one("SELECT COUNT(*) c FROM errors")["c"] == 1


def test_telegram_and_email_channels(app, db):
    posts, mails = [], []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            self.host = host

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            pass

        def login(self, user, password):
            mails.append(("login", user, password))

        def send_message(self, msg):
            mails.append(msg)

    app.settings = replace(app.settings, notify_telegram_chat_id="42", notify_email_to="yo@example.com",
                           notify_smtp_host="smtp.example.com", notify_smtp_user="yo")
    client = httpx.Client(transport=httpx.MockTransport(lambda r: posts.append(r) or httpx.Response(200)))
    _notification(db, kind="digest", key="digest:1")
    n = Notifier(app, client=client, smtp_factory=FakeSMTP,
                 env={"ROS_TELEGRAM_TOKEN": "bot", "ROS_SMTP_PASSWORD": "pw"})
    assert n.deliver_pending() == 2
    assert "/botbot/sendMessage" in str(posts[0].url) and json.loads(posts[0].content)["chat_id"] == "42"
    assert mails[0] == ("login", "yo", "pw") and mails[1]["Subject"] == "⚠ algo"


# ---------------------------------------------------------------- API
@pytest.fixture
def api(app, db):
    db.execute("INSERT INTO runs (kind, objective, status, created_at, updated_at, report_md) VALUES "
               "('research','sodio','completed',?,?, '# Informe')", (now_iso(), now_iso()))
    return create_app(app, token="secret")


def test_api_requires_auth_and_validates_host(api):
    c = TestClient(api, base_url="http://127.0.0.1")
    assert c.get("/api/v1/health").status_code == 401
    ok = c.get("/api/v1/health", headers={"Authorization": "Bearer secret"})
    assert ok.status_code == 200 and ok.json()["status"] == "ok"
    assert c.get("/api/v1/health", headers={"Authorization": "Bearer wrong"}).status_code == 401
    evil = TestClient(api, base_url="http://evil.example")
    assert evil.get("/api/v1/health", headers={"Authorization": "Bearer secret"}).status_code == 400


def test_api_reads_and_queues_research_idempotently(api, db):
    c = TestClient(api, base_url="http://127.0.0.1", headers={"Authorization": "Bearer secret"})
    assert c.get("/api/v1/runs").json()["items"][0]["objective"] == "sodio"
    assert c.get("/api/v1/runs/1/report").text == "# Informe"
    first = c.post("/api/v1/runs", json={"objective": "baterías de sodio"}, headers={"Idempotency-Key": "k1"})
    assert first.status_code == 202 and first.json()["status"] == "queued"
    again = c.post("/api/v1/runs", json={"objective": "baterías de sodio"}, headers={"Idempotency-Key": "k1"})
    assert again.status_code == 200 and again.json()["id"] == first.json()["id"]
    assert db.one("SELECT COUNT(*) c FROM runs WHERE status='queued'")["c"] == 1
    assert {x["id"] for x in c.get("/api/v1/connectors").json()["items"]} >= {"rss", "youtube_api", "x"}


def test_dashboard_login_code_is_single_use_and_cookie_cannot_mutate(api):
    c = TestClient(api, base_url="http://127.0.0.1")
    code = api.state.new_login_code()
    assert c.get(f"/login?code={code}", follow_redirects=False).status_code == 303
    assert c.get("/").status_code == 200 and "ROS" in c.get("/").text
    assert c.get("/runs/1").status_code == 200
    assert TestClient(api, base_url="http://127.0.0.1").get(f"/login?code={code}").status_code == 403
    assert c.post("/api/v1/runs", json={"objective": "algo nuevo"}).status_code == 401


def test_api_token_file_is_private(tmp_path):
    token = load_or_create_token(tmp_path)
    assert load_or_create_token(tmp_path) == token
    assert oct(os.stat(tmp_path / "api_token").st_mode & 0o777) == "0o600"


# ---------------------------------------------------------------- Obsidian
def test_obsidian_export_preserves_user_notes_and_detects_conflicts(db, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    db.execute("INSERT INTO runs (kind, objective, status, created_at, updated_at, report_md) VALUES "
               "('research','Baterías de sodio','completed',?,?, '# Informe v1')", (now_iso(), now_iso()))
    res = export_vault(db, vault)
    note = vault / "ROS" / "Research" / "0001 baterias-de-sodio.md"
    assert note.exists() and (vault / "ROS" / "Home.md").exists() and len(res.written) == 2
    assert export_vault(db, vault).written == []                      # idempotent

    note.write_text(note.read_text() + "Mi comentario personal.\n")   # user writes outside the managed block
    db.execute("UPDATE runs SET report_md='# Informe v2'")
    export_vault(db, vault)
    text = note.read_text()
    assert "# Informe v2" in text and "Mi comentario personal." in text

    note.write_text(text.replace("# Informe v2", "# Editado a mano"))  # user edits inside the managed block
    db.execute("UPDATE runs SET report_md='# Informe v3'")
    res = export_vault(db, vault)
    assert res.conflicts == ["ROS/Research/0001 baterias-de-sodio.md"]
    assert "# Editado a mano" in note.read_text()
    assert (note.parent / "0001 baterias-de-sodio.ros-conflict.md").exists()
    assert db.one("SELECT COUNT(*) c FROM publications")["c"] == 2


def test_obsidian_paths_cannot_escape_the_vault(tmp_path):
    vault = tmp_path / "vault"
    (vault / "ROS").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (vault / "ROS" / "link").symlink_to(outside)
    with pytest.raises(ValueError):
        safe_path(vault, "../escape.md")
    with pytest.raises(ValueError):
        safe_path(vault, "ROS/link/x.md")
