"""Watches end to end: only new content, no duplicates, correlation, alerts, digests, crash recovery."""

import re
from datetime import datetime, timezone

import pytest

from ros import offline
from ros.connectors.search import StaticSearchBackend
from ros.monitor.daemon import Daemon
from ros.monitor.digest import build_digest, evaluate_alerts
from ros.monitor.schedule import FixedClock, next_daily
from ros.monitor.service import WatchService
from ros.monitor.spec import AlertRules, DigestPolicy, SourceRef, WatchSpec, parse_digest, parse_source

FEED_A, FEED_B = "https://blog.example/feed", "https://news.example/rss"
LOREM = ("Los analistas del sector explican que la producción a gran escala reduce costes, mejora la cadena de "
         "suministro y abre nuevos mercados para el almacenamiento estacionario en redes eléctricas europeas. ")


def filler(seed: str, n: int = 40) -> str:
    """Distinct padding per item, so unrelated items are never near-duplicates of each other."""
    return " ".join(f"{seed}{k}" for k in range(n)) + ". "


def rss(items):
    body = "".join(f"<item><title>{t}</title><link>{l}</link><guid>{g}</guid><pubDate>{d}</pubDate>"
                   f"<description>{x}</description></item>" for g, t, l, x, d in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>Feed</title>{body}</channel></rss>'


def item(n, title, text, host="blog.example", day=1):
    return (f"{host}-{n}", title, f"https://{host}/p/{n}", text, f"Mon, 0{day} Oct 2026 10:00:00 GMT")


def model(purpose, system, user, schema):
    if purpose == "triage":
        title = re.search(r"^TITLE: (.*)$", user, re.M).group(1)
        low = re.search(r"<external_content[^>]*>(.*)</external_content>", user, re.S).group(1).lower()
        relevant = "sodio" in low
        return {"relevant": relevant, "relevance": 0.9 if relevant else 0.1,
                "importance": 0.95 if "récord" in low else 0.6, "title": title, "summary": f"resumen: {title}",
                "claim_type": "rumor" if "rumor" in low else "fact",
                "labels": ["lanzamiento"] if "lanza" in low else [],
                "entities": [e for e in ("CATL", "BYD", "Northvolt") if e in user],
                "claims": [{"text": f"afirma: {title}", "type": "fact", "quote": "q", "confidence": 0.8}],
                "why": "afecta al mercado", "injection_suspected": "ignore previous" in low}
    return offline.HANDLERS[purpose](user)


@pytest.fixture
def feeds(web):
    web.add(FEED_A, rss([item(1, "CATL lanza una batería de sodio", "CATL presenta celdas de sodio. " + LOREM * 2),
                         item(2, "Receta de paella", "Nada que ver con baterías. " + filler("paella"))]),
            content_type="application/rss+xml")
    web.add(FEED_B, rss([item(1, "BYD invierte en sodio", "BYD anuncia una planta de sodio en Hungría. " + "Otra "
                               "explicación distinta sobre fábricas, empleo y subvenciones públicas. " * 4,
                               host="news.example")]), content_type="application/rss+xml")
    return web


def spec(**changes):
    base = dict(name="sodio", objective="Novedades sobre baterías de sodio",
                sources=[SourceRef(kind="rss", locator=FEED_A), SourceRef(kind="rss", locator=FEED_B)],
                alerts=AlertRules(min_importance=0.9, min_independent_sources=2), digest=DigestPolicy(mode="manual"))
    base.update(changes)
    return WatchSpec(**base)


@pytest.fixture
def app(make_app):
    return make_app(model, StaticSearchBackend(lambda q, n: []))


def test_only_new_content_and_no_duplicates(app, feeds, db):
    service = WatchService(app)
    wid = service.create(spec())
    first = service.run_watch(wid)
    assert first["status"] == "completed" and first["new"] == 3 and first["events"] == 2
    assert db.one("SELECT COUNT(*) c FROM watch_items WHERE watch_id=? AND analysis='done'", (wid,))["c"] == 3

    second = service.run_watch(wid)                     # nothing changed
    assert second["new"] == 0 and second["modified"] == 0 and second["events"] == 0
    assert db.one("SELECT COUNT(*) c FROM events")["c"] == 2

    feeds.add(FEED_A, rss([item(1, "CATL lanza una batería de sodio", "CATL presenta celdas de sodio. " + LOREM * 2),
                           item(2, "Receta de paella", "Nada que ver con baterías. " + filler("paella")),
                           item(3, "Northvolt bate un récord con sodio", "Northvolt logra un récord. " + filler("nv"))]),
              content_type="application/rss+xml")
    third = service.run_watch(wid)
    assert third["new"] == 1 and third["events"] == 1
    triaged = [u for p, u in app.llm("worker").calls if p == "triage"]
    assert len(triaged) == 4                              # every item analysed exactly once

    # An edited item is detected as modified and analysed with its diff.
    feeds.add(FEED_A, rss([item(1, "CATL lanza una batería de sodio", "CATL presenta celdas de sodio con 200 Wh/kg. "
                                + LOREM * 2)]), content_type="application/rss+xml")
    fourth = service.run_watch(wid)
    assert fourth["modified"] == 1
    last = [u for p, u in app.llm("worker").calls if p == "triage"][-1]
    assert "CHANGE: modified" in last and "200 Wh/kg" in last
    assert db.one("SELECT COUNT(*) c FROM events WHERE change='modified'")["c"] == 1


def test_first_sync_baseline_limits_backfill(app, feeds, db):
    service = WatchService(app)
    wid = service.create(spec(backfill=1, sources=[SourceRef(kind="rss", locator=FEED_A)]))
    summary = service.run_watch(wid)
    assert summary["new"] == 1 and summary["baseline"] == 1
    assert db.one("SELECT COUNT(*) c FROM watch_items WHERE reason LIKE 'línea base%'")["c"] == 1


def test_cross_source_duplicate_is_corroboration_not_a_new_event(app, feeds, db):
    copy = item(9, "CATL lanza una batería de sodio", "CATL presenta celdas de sodio. " + LOREM * 2, host="news.example")
    copy = (copy[0], copy[1], "https://blog.example/p/1", copy[3], copy[4])      # same URL syndicated
    feeds.add(FEED_B, rss([copy]), content_type="application/rss+xml")
    service = WatchService(app)
    wid = service.create(spec())
    s = service.run_watch(wid)
    assert s["duplicates"] == 1 and s["events"] == 1
    event = db.one("SELECT id FROM events")["id"]
    assert db.one("SELECT COUNT(*) c FROM event_items WHERE event_id=?", (event,))["c"] == 2


def test_two_watches_on_the_same_feed_each_see_the_items(app, feeds, db):
    service = WatchService(app)
    a = service.create(spec(name="a", sources=[SourceRef(kind="rss", locator=FEED_A)]))
    b = service.create(spec(name="b", sources=[SourceRef(kind="rss", locator=FEED_A)]))
    assert service.run_watch(a)["events"] == 1
    assert service.run_watch(b)["events"] == 1


def test_correlation_alerts_and_idempotent_digest(app, feeds, db):
    feeds.add(FEED_B, rss([item(1, "CATL confirma su batería de sodio", "Según CATL la batería de sodio llega en "
                                "2027 a coches eléctricos. " + "Las reacciones del mercado fueron positivas. " * 5,
                                host="news.example")]), content_type="application/rss+xml")
    service = WatchService(app)
    wid = service.create(spec())
    s = service.run_watch(wid)
    assert s["events"] == 2
    clusters = db.all("SELECT * FROM clusters WHERE watch_id=?", (wid,))
    assert len(clusters) == 1                                       # same matter, two independent sources
    alerts = db.all("SELECT * FROM notifications WHERE kind='alert'")
    assert len(alerts) == 1 and "2 fuentes independientes" in alerts[0]["body"]
    assert evaluate_alerts(db, wid, service.spec_of(service.by_id(wid)), now=datetime.now(timezone.utc)) == []

    now = datetime.now(timezone.utc)
    digest_id = build_digest(app, wid, now=now)
    md = db.one("SELECT markdown FROM digests WHERE id=?", (digest_id,))["markdown"]
    for section in ("## Grupos temáticos", "## Cronología", "## Fuentes", "## Cobertura", "news.example",
                    "blog.example", "Periodo"):
        assert section in md
    assert build_digest(app, wid, now=now) is None                  # nothing new: no duplicate digest
    assert db.one("SELECT COUNT(*) c FROM notifications WHERE kind='digest'")["c"] == 1


def test_importance_alert_and_daily_attention_budget(app, feeds, db):
    feeds.add(FEED_A, rss([item(i, f"Récord de sodio número {i}", f"Un récord de sodio distinto {i}. " + filler(f"r{i}x"))
                           for i in range(1, 4)]), content_type="application/rss+xml")
    service = WatchService(app)
    wid = service.create(spec(sources=[SourceRef(kind="rss", locator=FEED_A)],
                              alerts=AlertRules(min_importance=0.9, max_per_day=2)))
    service.run_watch(wid)
    assert db.one("SELECT COUNT(*) c FROM notifications WHERE kind='alert'")["c"] == 2
    assert db.one("SELECT COUNT(*) c FROM events WHERE alerted=2")["c"] == 1   # over budget: goes to the digest


def test_crash_between_ingest_and_analysis_recovers_without_refetch(make_app, feeds, db):
    state = {"crash": True}

    def crashing(purpose, system, user, schema):
        if purpose == "triage" and state.pop("crash", False):
            raise KeyboardInterrupt  # the process dies after transaction A
        return model(purpose, system, user, schema)

    app = make_app(crashing, StaticSearchBackend(lambda q, n: []))
    service = WatchService(app)
    wid = service.create(spec())
    with pytest.raises(KeyboardInterrupt):
        service.run_watch(wid)
    assert db.one("SELECT COUNT(*) c FROM watch_items WHERE analysis='pending'")["c"] == 3
    fetches = len(feeds.requests)
    service.analyze_pending(wid)
    assert len(feeds.requests) == fetches                        # nothing downloaded again
    assert db.one("SELECT COUNT(*) c FROM watch_items WHERE analysis='pending'")["c"] == 0
    assert db.one("SELECT COUNT(*) c FROM events")["c"] == 2


def test_daemon_schedules_watches_and_digests(app, feeds, db):
    clock = FixedClock(datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc))
    service = WatchService(app, clock=clock)
    service.create(spec(every_minutes=60, timezone="Europe/Madrid", digest=DigestPolicy(mode="daily", at="20:00")),
                   enabled=True)
    daemon = Daemon(app, clock=clock, echo=lambda s: None, sleep=lambda s: None)
    assert daemon.tick()["watches"] == 1
    clock.advance(minutes=30)
    assert daemon.tick()["watches"] == 0                         # not due yet
    clock.advance(minutes=31)
    assert daemon.tick()["watches"] == 1
    clock.current = datetime(2026, 10, 5, 18, 0, 30, tzinfo=timezone.utc)   # 20:00:30 in Madrid (CEST)
    done = daemon.tick()
    assert done["digests"] == 1
    w = db.one("SELECT next_digest_at FROM watches")
    assert w["next_digest_at"] == "2026-10-06T18:00:00+00:00"
    other = Daemon(app, clock=clock, echo=lambda s: None)
    with pytest.raises(RuntimeError):
        other.tick()                                             # only one daemon per database
    daemon.release()


def test_daemon_expires_watches_and_resumes_queued_research(app, feeds, db, web):
    from ros.research.engine import ResearchEngine
    clock = FixedClock(datetime.now(timezone.utc))
    service = WatchService(app, clock=clock)
    service.create(spec(expires_at="2020-01-01T00:00:00+00:00"))
    db.execute("UPDATE watches SET enabled=1")
    run_id = ResearchEngine(app).create("baterías de sodio", status="queued")
    done = Daemon(app, clock=clock, echo=lambda s: None).tick()
    assert done["expired"] == 1 and done["research"] == 1
    assert db.one("SELECT enabled FROM watches")["enabled"] == 0
    # It ran to an end state (no search results here, so there is no evidence: 'failed' is the honest outcome).
    assert db.one("SELECT status FROM runs WHERE id=?", (run_id,))["status"] == "failed"


def test_failing_source_opens_circuit_and_asks_for_attention(app, web, db):
    web.add(FEED_A, "boom", status=500)
    service = WatchService(app, sleep=lambda s: None)
    wid = service.create(spec(sources=[SourceRef(kind="rss", locator=FEED_A)]))
    for _ in range(3):
        assert service.run_watch(wid)["status"] == "failed"
    assert db.one("SELECT failures, open_until FROM source_health")["open_until"] is not None
    skipped = service.run_watch(wid)
    assert skipped["sources_skipped"] == 1
    assert db.one("SELECT COUNT(*) c FROM notifications WHERE kind='attention'")["c"] >= 1
    assert db.one("SELECT COUNT(*) c FROM errors WHERE watch_id=?", (wid,))["c"] == 3


def test_spec_versions_and_parsers(app, db):
    service = WatchService(app)
    s = spec()
    service.create(s)
    v = service.update("sodio", s.model_copy(update={"every_minutes": 30}))
    assert v == 2 and len(service.versions("sodio")) == 2
    assert parse_digest("weekly@vie@18:30").weekday == 4 and parse_digest("threshold:3").min_events == 3
    assert parse_source("https://www.reddit.com/r/python/").kind == "reddit"
    assert parse_source("rss:https://x.example/feed").locator == "https://x.example/feed"
    assert parse_source("https://x.example/blog/feed").kind == "rss"
    with pytest.raises(ValueError):
        WatchSpec(name="Mal Nombre!", objective="x", sources=[SourceRef(kind="web", locator="https://a.example")])


def test_local_schedule_handles_daylight_saving():
    # Spring forward in Madrid (2026-03-29): 02:30 does not exist -> first valid instant, 03:00 CEST.
    after = datetime(2026, 3, 28, 23, 0, tzinfo=timezone.utc)
    assert next_daily(after, "02:30", "Europe/Madrid") == datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc)
    # Fall back (2026-10-25): 02:30 happens twice -> runs once, at the first occurrence (CEST).
    after = datetime(2026, 10, 24, 23, 0, tzinfo=timezone.utc)
    assert next_daily(after, "02:30", "Europe/Madrid") == datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)


def test_copy_of_an_item_only_another_watch_has_is_still_analysed(app, feeds, db):
    copy = item(9, "CATL lanza una batería de sodio", "CATL presenta celdas de sodio. " + LOREM * 2, host="news.example")
    feeds.add(FEED_B, rss([(copy[0], copy[1], "https://blog.example/p/1", copy[3], copy[4])]),
              content_type="application/rss+xml")
    service = WatchService(app)
    a = service.create(spec(name="a", sources=[SourceRef(kind="rss", locator=FEED_A)]))
    b = service.create(spec(name="b", sources=[SourceRef(kind="rss", locator=FEED_B)]))
    assert service.run_watch(a)["events"] == 1
    assert service.run_watch(b)["events"] == 1          # watch b never saw the original: not a duplicate for it


def test_deleting_a_watch_keeps_execution_history(app, feeds, db):
    service = WatchService(app)
    wid = service.create(spec())
    service.run_watch(wid)
    service.delete("sodio")
    assert db.one("SELECT COUNT(*) c FROM watches")["c"] == 0
    assert db.one("SELECT COUNT(*) c FROM runs WHERE kind='monitor' AND watch_id IS NULL")["c"] == 1
