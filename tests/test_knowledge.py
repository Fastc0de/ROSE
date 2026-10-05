"""Knowledge memory, evidence quality and research control (cancel, shutdown, source-cap policy)."""

import pytest

from ros.budget import Budget
from ros.db import now_iso
from ros.knowledge import (feedback_adjustment, find_near_duplicate, prune, record_feedback, search_corpus,
                           source_reputation)
from ros.research.engine import ResearchEngine

from .test_research_engine import A, B, scripted_model, search_engine, site  # noqa: F401 (fixture)

STORY = ("El fabricante anunció hoy una nueva celda de sodio con una densidad de 175 Wh/kg que llegará a coches "
         "urbanos el próximo año, según el comunicado oficial publicado en su web corporativa y recogido por la "
         "prensa especializada del sector del automóvil eléctrico europeo y asiático.")


def _item(db, ext, text, url=None, meta="{}"):
    sid = db.upsert_source("site", "example.com", "public_web")
    ts = now_iso()
    return db.insert("items", {"source_id": sid, "external_id": ext, "url": url or f"https://example.com/{ext}",
                               "title": ext, "text": text, "first_seen_at": ts, "last_seen_at": ts, "meta_json": meta})


def test_near_duplicates_are_detected_but_kept(db):
    first = _item(db, "a", STORY)
    second = _item(db, "b", STORY.replace("hoy", "este lunes") + " Fuente: agencias.")
    assert find_near_duplicate(db, db.one("SELECT text FROM items WHERE id=?", (second,))["text"],
                               exclude_item_id=second) == first
    assert find_near_duplicate(db, "Un texto completamente diferente sobre recetas de cocina mediterránea, con "
                                   "ingredientes de temporada, tiempos de cocción y consejos para servir platos "
                                   "tradicionales en familia durante los fines de semana largos.") is None


def test_prune_keeps_metadata_and_is_audited(db):
    item = _item(db, "old", STORY)
    db.execute("UPDATE items SET last_seen_at='2020-01-01T00:00:00+00:00' WHERE id=?", (item,))
    db.insert("claims", {"item_id": item, "text": "afirmación", "claim_type": "fact", "created_at": now_iso()})
    preview = prune(db, 30, dry_run=True)
    assert preview["items"] == 1 and db.one("SELECT text FROM items")["text"]
    prune(db, 30)
    row = db.one("SELECT text, text_pruned_at, content_hash, url FROM items")
    assert row["text"] is None and row["text_pruned_at"] and row["url"]
    assert db.one("SELECT COUNT(*) c FROM claims")["c"] == 1
    assert db.one("SELECT items FROM prune_log")["items"] == 1


def test_reputation_dimensions_and_source_feedback(db):
    _item(db, "p", STORY, "https://gov.example/x", '{"source_kind": "primary", "authority": "high"}')
    _item(db, "c", STORY + " x", "https://shop.example/x",
          '{"source_kind": "commercial", "authority": "low", "conflicts_of_interest": "vende el producto"}')
    reps = {r["host"]: r for r in source_reputation(db)}
    assert reps["gov.example"]["score"] > reps["shop.example"]["score"]
    assert reps["shop.example"]["conflicts_of_interest"] == 1
    record_feedback(db, "source", "shop.example", "less")
    assert {r["host"]: r for r in source_reputation(db)}["shop.example"]["feedback"] == -1
    with pytest.raises(ValueError):
        record_feedback(db, "event", 999, "more")


def test_feedback_adjusts_importance_transparently(db):
    ts = now_iso()
    wid = db.insert("watches", {"name": "w", "spec_json": "{}", "created_at": ts, "updated_at": ts})
    item = _item(db, "e", STORY)
    ev = db.insert("events", {"watch_id": wid, "item_id": item, "created_at": ts, "title": "t", "summary": "s",
                              "change": "new", "relevance": 1, "importance": 1, "claim_type": "fact",
                              "labels_json": '["precio"]'})
    record_feedback(db, "event", ev, "known")
    factor, reasons = feedback_adjustment(db, wid, ["precio"], [], "example.com")
    assert factor == pytest.approx(0.85) and "known" in reasons[0]
    assert feedback_adjustment(db, wid, ["otra"], [], "example.com")[0] == 1.0


def test_prior_research_is_context_not_evidence(make_app, site, db):  # noqa: F811
    app = make_app(scripted_model, search_engine())
    first = ResearchEngine(app)
    run1 = first.create("baterías de sodio", exclude_domains=("evil.example",))
    assert first.run(run1) == "completed"
    second = ResearchEngine(app)
    run2 = second.create("baterías de sodio costes")
    second.prepare(run2)
    plan_prompt = [u for p, u in app.llm("orchestrator").calls if p == "plan"][-1]
    assert "Already known from earlier research" in plan_prompt and f"research #{run1}" in plan_prompt
    assert db.one("SELECT COUNT(*) c FROM claims WHERE run_id=?", (run2,))["c"] == 0
    found = search_corpus(db, "sodio")
    assert found["claims"] and found["items"]
    assert search_corpus(db, "fabricarlas")["findings"]


def test_cancel_keeps_a_partial_report(make_app, site, db):  # noqa: F811
    app = make_app(scripted_model, search_engine())
    engine = ResearchEngine(app)
    run_id = engine.create("baterías de sodio")
    engine.prepare(run_id)
    assert engine.cancel(run_id) == "cancelled"
    report = db.one("SELECT report_md, status FROM runs WHERE id=?", (run_id,))
    assert report["status"] == "cancelled" and "Cancelada" in report["report_md"]
    assert engine.run(run_id) == "cancelled"


def test_cancellation_requested_while_running_stops_at_a_checkpoint(make_app, site, db):  # noqa: F811
    def model(purpose, system, user, schema):
        if purpose == "analyze":
            db.execute("UPDATE runs SET cancel_requested=1")
        return scripted_model(purpose, system, user, schema)

    engine = ResearchEngine(make_app(model, search_engine()))
    run_id = engine.create("baterías de sodio", exclude_domains=("evil.example",))
    assert engine.run(run_id) == "cancelled"
    assert db.one("SELECT COUNT(*) c FROM rounds WHERE run_id=?", (run_id,))["c"] == 1


def test_shutdown_request_requeues_the_run(make_app, site, db):  # noqa: F811
    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return calls["n"] > 3

    app = make_app(scripted_model, search_engine())
    run_id = ResearchEngine(app).create("baterías de sodio", exclude_domains=("evil.example",))
    assert ResearchEngine(app, stop_requested=stop).run(run_id) == "queued"
    assert ResearchEngine(app).run(run_id) == "completed"


def test_source_cap_after_covering_the_plan_is_not_partial(make_app, site, db):  # noqa: F811
    def model(purpose, system, user, schema):
        data = scripted_model(purpose, system, user, schema)
        if purpose == "analyze":
            data.update(coverage=0.8, should_stop=False)
        return data

    engine = ResearchEngine(make_app(model, search_engine()))
    run_id = engine.create("baterías de sodio", Budget(max_sources=3), exclude_domains=("evil.example",))
    assert engine.run(run_id) == "completed"
    note = db.one("SELECT outcome_note FROM runs WHERE id=?", (run_id,))["outcome_note"]
    assert "tope de fuentes" in note


def test_a_run_cannot_execute_in_two_processes(make_app, site, db):  # noqa: F811
    from ros.research.engine import RunBusy
    app = make_app(scripted_model, search_engine())
    run_id = ResearchEngine(app).create("baterías de sodio")
    assert db.acquire_lock(f"run:{run_id}", "otro-proceso", 600)
    with pytest.raises(RunBusy):
        ResearchEngine(app).run(run_id)
