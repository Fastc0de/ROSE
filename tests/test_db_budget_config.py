import sqlite3

import pytest

from ros import db as dbmod
from ros.budget import Budget, Ledger
from ros.config import load_settings
from ros.db import Database, now_iso
from ros.errors import BudgetExhausted


# -- database -------------------------------------------------------------

def _run(db: Database) -> int:
    ts = now_iso()
    return db.insert("runs", {"kind": "research", "objective": "x", "status": "planned", "created_at": ts,
                              "updated_at": ts})


def test_reopening_database_is_idempotent(tmp_path):
    path = tmp_path / "ros.db"
    Database(path).close()
    db = Database(path)
    assert db.one("SELECT COUNT(*) c FROM schema_migrations")["c"] == len(dbmod.MIGRATIONS)
    assert db.one("PRAGMA journal_mode")[0] == "wal"
    assert db.one("PRAGMA foreign_keys")[0] == 1


def test_refuses_checksum_drift(tmp_path, monkeypatch):
    path = tmp_path / "ros.db"
    Database(path).close()
    version, sql = dbmod.MIGRATIONS[0]
    monkeypatch.setattr(dbmod, "MIGRATIONS", [(version, sql + "\n-- edited"), *dbmod.MIGRATIONS[1:]])
    with pytest.raises(RuntimeError, match="checksum drift"):
        Database(path)


def test_refuses_newer_schema(tmp_path):
    path = tmp_path / "ros.db"
    Database(path).close()
    con = sqlite3.connect(path)
    con.execute("INSERT INTO schema_migrations VALUES (999, 'x', 'now')")
    con.commit()
    con.close()
    with pytest.raises(RuntimeError, match="newer"):
        Database(path)


def test_transaction_rolls_back(db):
    with pytest.raises(ZeroDivisionError):
        with db.tx():
            _run(db)
            1 / 0
    assert db.one("SELECT COUNT(*) c FROM runs")["c"] == 0


def test_fulltext_search_over_items_and_claims(db):
    sid = db.upsert_source("site", "example.com", "public_web")
    ts = now_iso()
    item = db.insert("items", {"source_id": sid, "external_id": "a", "title": "Baterías de sodio",
                               "text": "La densidad energética mejora cada año", "first_seen_at": ts,
                               "last_seen_at": ts})
    db.insert("claims", {"item_id": item, "text": "El litio sube de precio", "claim_type": "fact",
                         "created_at": ts})
    assert [r["rowid"] for r in db.all("SELECT rowid FROM items_fts WHERE items_fts MATCH 'densidad'")] == [item]
    assert db.one("SELECT COUNT(*) c FROM claims_fts WHERE claims_fts MATCH 'litio'")["c"] == 1
    db.execute("UPDATE items SET text='otra cosa' WHERE id=?", (item,))
    assert db.one("SELECT COUNT(*) c FROM items_fts WHERE items_fts MATCH 'densidad'")["c"] == 0


def test_upsert_source_is_stable(db):
    a = db.upsert_source("rss", "https://x.com/feed", "feed")
    b = db.upsert_source("rss", "https://x.com/feed", "feed", title="X")
    assert a == b and db.one("SELECT title FROM sources WHERE id=?", (a,))["title"] == "X"


def test_backup_is_consistent(db, tmp_path):
    _run(db)
    dest = db.backup(tmp_path / "bk" / "copy.db")
    copy = Database(dest)
    assert copy.one("SELECT COUNT(*) c FROM runs")["c"] == 1


def test_lock_excludes_other_owner(db):
    assert db.acquire_lock("daemon", "a", ttl=60)
    assert not db.acquire_lock("daemon", "b", ttl=60)
    db.release_lock("daemon", "a")
    assert db.acquire_lock("daemon", "b", ttl=60)


# -- budget ---------------------------------------------------------------

def test_reservation_rejects_calls_over_token_cap(db):
    ledger = Ledger(db, _run(db), Budget(max_tokens=10_000, final_reserve=0.0))
    ledger.reserve_llm(4_000, 4_000, 0.01)
    with pytest.raises(BudgetExhausted) as info:
        ledger.reserve_llm(1_000, 2_000, 0.01)  # 8k reserved + 3k > 10k
    assert info.value.limit == "tokens"


def test_settled_usage_counts_against_cost_cap(db):
    ledger = Ledger(db, _run(db), Budget(max_cost_usd=1.0, final_reserve=0.0))
    ledger.reserve_llm(10, 10, 0.5)
    ledger.settle_llm(10, 10, 0.5, purpose="t", model="m", input_tokens=10, output_tokens=10, cost=0.9)
    with pytest.raises(BudgetExhausted) as info:
        ledger.reserve_llm(10, 10, 0.2)
    assert info.value.limit == "cost"
    assert ledger.totals().cost_usd == pytest.approx(0.9)


def test_final_reserve_is_only_spent_on_the_report(db):
    ledger = Ledger(db, _run(db), Budget(max_cost_usd=1.0, final_reserve=0.3))
    with pytest.raises(BudgetExhausted):
        ledger.reserve_llm(10, 10, 0.8)   # above the 0.7 working cap
    ledger.enter_final_phase()
    ledger.reserve_llm(10, 10, 0.8)       # the reserve is available for the final report


def test_elapsed_time_survives_restart(db, monkeypatch):
    run_id = _run(db)
    clock = [100.0]
    monkeypatch.setattr("ros.budget.time.monotonic", lambda: clock[0])
    first = Ledger(db, run_id, Budget(max_minutes=1))
    clock[0] += 45
    first.checkpoint_time()
    second = Ledger(db, run_id, Budget(max_minutes=1))   # new process, new monotonic origin
    clock[0] += 20
    assert second.elapsed_s() == pytest.approx(65)
    with pytest.raises(BudgetExhausted) as info:
        second.check_time()
    assert info.value.limit == "time"


# -- configuration --------------------------------------------------------

@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ROS_HOME", str(tmp_path / ".ros"))
    for var in ("ROS_DB", "ROS_LLM", "ROS_SEARCH", "ROS_ORCHESTRATOR_MODEL", "ROS_WORKER_MODEL",
                "ANTHROPIC_WORKSPACE_ID", "ROS_OPENROUTER_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_config_precedence(isolated, monkeypatch):
    (isolated / ".config" / "ros").mkdir(parents=True)
    (isolated / ".config" / "ros" / "config.toml").write_text('orchestrator_model = "global"\nvalidator_effort = "low"\n')
    (isolated / "ros.toml").write_text('orchestrator_model = "project"\n[budget]\nmax_rounds = 7\n')
    explicit = isolated / "explicit.toml"
    explicit.write_text('orchestrator_model = "explicit"\nsearch_backend = "brave"\n')
    monkeypatch.setenv("ROS_SEARCH", "searxng")
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_123")
    s = load_settings(str(explicit), {"budget": {"max_sources": 3}})
    assert s.validator_effort == "low"                # user-global file
    assert s.orchestrator_model == "explicit"         # --config beats ./ros.toml
    assert s.anthropic_workspace_id == "wrkspc_123"
    assert s.search_backend == "searxng"     # environment beats files
    assert s.budget.max_rounds == 7 and s.budget.max_sources == 3   # budgets merge field by field
    assert s.db_path == str(isolated / ".ros" / "ros.db")


def test_config_rejects_unknown_keys(isolated):
    (isolated / "ros.toml").write_text('modle = "typo"\n')
    with pytest.raises(ValueError, match="modle"):
        load_settings()


def test_missing_explicit_config_fails(isolated):
    with pytest.raises(FileNotFoundError):
        load_settings("nope.toml")


def test_default_model_roles(isolated):
    s = load_settings()
    assert s.llm == "openrouter"
    for role in ("orchestrator", "validator", "worker"):
        assert s.role(role) == ("spastealth/space-bunny-alpha", "")
    with pytest.raises(ValueError):
        s.role("boss")


def test_role_models_per_provider(isolated, monkeypatch):
    (isolated / "ros.toml").write_text('worker_model = "openai/gpt-x"\n')
    s = load_settings()
    assert s.role("worker") == ("openai/gpt-x", "") and s.role("validator")[0] == "spastealth/space-bunny-alpha"
    monkeypatch.setenv("ROS_LLM", "anthropic")
    monkeypatch.setenv("ROS_OPENROUTER_MODEL", "ignored/when-anthropic")
    s = load_settings()
    assert s.role("orchestrator") == ("claude-opus-5-5", "medium")
    assert s.role("validator") == ("claude-sonnet-5-5", "medium")
    assert s.role("worker") == ("openai/gpt-x", "")


def test_lock_of_a_dead_process_on_this_host_is_released(db):
    import socket
    import subprocess
    import sys
    from ros.db import lock_owner
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()                                              # a pid that no longer exists
    dead = f"{socket.gethostname()}:{child.pid}:abcd1234"
    assert db.acquire_lock("run:1", dead, ttl=600)
    assert db.acquire_lock("run:1", lock_owner(), ttl=600)    # taken over at once, no 10-minute wait
    assert not db.acquire_lock("run:1", f"otra-maquina:{child.pid}:x", ttl=600)   # other hosts: honour the TTL
