"""Cross-source correlation: related events become one cluster (one matter).

Two steps, cheap first:
1. heuristic grouping by shared entities and terms inside the watch's time window;
2. the model confirms multi-event groups, drops members that do not belong, and explains the
   relation (what is confirmed, what is uncertain, contradictions, what to watch next).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Callable

from ..budget import Ledger
from ..connectors.feeds import host_of
from ..db import dumps, loads, now_iso
from ..errors import BudgetExhausted, RosError
from ..registry import App
from . import prompts
from .schedule import iso
from .spec import WatchSpec

MATCH_THRESHOLD = 0.34
STOPWORDS = set("""
para como pero más este esta estos estas sobre entre desde hasta tras cuando donde porque también
según tiene tienen será sido están puede pueden nuevo nueva nuevos nuevas otro otra otros otras cada
with from that this these those have has will been about into over after before their there what which
""".split())


def event_terms(title: str, labels: list[str], entities: list[str]) -> dict[str, list[str]]:
    ents = {e.strip().lower() for e in entities if e.strip()}
    words = {w for w in re.findall(r"\w+", title.lower()) if len(w) >= 4 and w not in STOPWORDS and not w.isdigit()}
    words |= {l.strip().lower() for l in labels if l.strip()}
    return {"e": sorted(ents), "w": sorted(words)}


def _overlap(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def match_score(a: dict, b: dict) -> float:
    ea, eb, wa, wb = set(a["e"]), set(b["e"]), set(a["w"]), set(b["w"])
    if not (ea & eb) and len(wa & wb) < 2:
        return 0.0  # require a shared entity or at least two shared terms
    return 0.6 * _overlap(ea, eb) + 0.4 * _overlap(wa, wb)


def independent_hosts(db, cluster_id: int) -> set[str]:
    rows = db.all("""SELECT i.url, s.locator FROM events e JOIN event_items ei ON ei.event_id=e.id
                     JOIN items i ON i.id=ei.item_id LEFT JOIN sources s ON s.id=i.source_id
                     WHERE e.cluster_id=?""", (cluster_id,))
    return {host_of(r["url"]) or (r["locator"] or "") for r in rows} - {""}


def correlate(app: App, watch_id: int, spec: WatchSpec, ledger: Ledger, run_id: int, *, now: datetime,
              echo: Callable[[str], None] = lambda s: None) -> list[int]:
    db = app.db
    window_start = iso(now - timedelta(hours=spec.group_window_hours))
    # Clusters whose matter went quiet are closed: later news starts a new matter.
    db.execute("UPDATE clusters SET status='closed' WHERE watch_id=? AND status='open' AND last_event_at < ?",
               (watch_id, window_start))
    clusters = {r["id"]: {"terms": loads(r["terms_json"], {"e": [], "w": []}), "row": r}
                for r in db.all("SELECT * FROM clusters WHERE watch_id=? AND status='open'", (watch_id,))}
    new_events = db.all("SELECT * FROM events WHERE watch_id=? AND cluster_id IS NULL ORDER BY created_at, id", (watch_id,))
    touched: set[int] = set()
    for ev in new_events:
        terms = event_terms(ev["title"], loads(ev["labels_json"], []), loads(ev["entities_json"], []))
        best_id, best = None, 0.0
        for cid, c in clusters.items():
            score = match_score(terms, c["terms"])
            if score > best:
                best_id, best = cid, score
        ts = now_iso()
        with db.tx():
            if best_id is not None and best >= MATCH_THRESHOLD:
                c = clusters[best_id]
                c["terms"] = {"e": sorted(set(c["terms"]["e"]) | set(terms["e"])),
                              "w": sorted(set(c["terms"]["w"]) | set(terms["w"]))}
                db.execute("UPDATE clusters SET terms_json=?, last_event_at=?, updated_at=? WHERE id=?",
                           (dumps(c["terms"]), ev["created_at"], ts, best_id))
                cid = best_id
            else:
                cid = db.insert("clusters", {"watch_id": watch_id, "title": ev["title"], "summary": ev["summary"],
                                             "created_at": ts, "updated_at": ts, "terms_json": dumps(terms),
                                             "last_event_at": ev["created_at"], "status": "open"})
                clusters[cid] = {"terms": terms, "row": None}
            db.execute("UPDATE events SET cluster_id=? WHERE id=?", (cid, ev["id"]))
        touched.add(cid)
    for cid in sorted(touched):
        _confirm(app, cid, spec, ledger, run_id, echo)
    return sorted(touched)


def _confirm(app: App, cid: int, spec: WatchSpec, ledger: Ledger, run_id: int, echo) -> None:
    db = app.db
    cluster = db.one("SELECT * FROM clusters WHERE id=?", (cid,))
    events = db.all("SELECT * FROM events WHERE cluster_id=? ORDER BY created_at, id", (cid,))
    if len(events) < 2 or cluster["analyzed_events"] >= len(events):
        if len(events) == 1:
            db.execute("UPDATE clusters SET title=?, summary=?, analyzed_events=1 WHERE id=?",
                       (events[0]["title"], events[0]["summary"], cid))
        return
    lines = []
    for e in events:
        hosts = sorted({host_of(r["url"]) for r in db.all(
            "SELECT i.url FROM event_items ei JOIN items i ON i.id=ei.item_id WHERE ei.event_id=?", (e["id"],))} - {""})
        lines.append(f"E{e['id']} [{e['claim_type']}, importance {e['importance']:.2f}, {e['created_at'][:16]}, "
                     f"sources: {', '.join(hosts) or 'unknown'}] {e['title']}: {e['summary']}")
    user = (f"Watch interest:\n{spec.objective}\n\nCandidate group of events (untrusted summaries):\n<events>\n"
            + "\n".join(lines) + "\n</events>")
    try:
        data = app.llm(prompts.STAGE_ROLE["cluster"]).complete_json(
            purpose="cluster", system=prompts.CLUSTER_SYSTEM, user=user, schema=prompts.CLUSTER_SCHEMA,
            max_tokens=prompts.MAX_TOKENS["cluster"], ledger=ledger)
    except BudgetExhausted:
        raise
    except RosError as exc:
        if exc.fatal:
            raise
        db.record_error(run_id=run_id, watch_id=cluster["watch_id"], kind=exc.kind.value, subject=f"cluster {cid}",
                        message=exc.message, impact="grupo formado solo por heurística, sin explicación del modelo")
        return
    ids = {e["id"] for e in events}
    keep = {i for i in data["member_event_ids"] if i in ids} or ids
    ts = now_iso()
    with db.tx():
        for e in events:
            if e["id"] in keep:
                continue
            # Not the same matter: it becomes its own cluster, nothing is discarded.
            terms = event_terms(e["title"], loads(e["labels_json"], []), loads(e["entities_json"], []))
            solo = db.insert("clusters", {"watch_id": cluster["watch_id"], "title": e["title"], "summary": e["summary"],
                                          "created_at": ts, "updated_at": ts, "terms_json": dumps(terms),
                                          "last_event_at": e["created_at"], "status": "open", "analyzed_events": 1})
            db.execute("UPDATE events SET cluster_id=? WHERE id=?", (solo, e["id"]))
        analysis = {k: data[k] for k in ("relation", "confirmed", "uncertain", "contradictions", "why_relevant",
                                         "watch_next", "importance")}
        db.execute("UPDATE clusters SET title=?, summary=?, analysis_json=?, analyzed_events=?, updated_at=? WHERE id=?",
                   (data["title"][:300], data["summary"][:3000], dumps(analysis), len(keep), ts, cid))
    if len(keep) > 1:
        echo(f"  ⧉ {data['title'][:80]} ({len(keep)} eventos relacionados)")
