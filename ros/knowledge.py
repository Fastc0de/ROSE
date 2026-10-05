"""Knowledge memory and evidence quality.

- Near-duplicate detection (shingle similarity) that keeps every member and its origin.
- Prior knowledge: findings of earlier research reused as context, never as fresh evidence.
- Source reputation by separate dimensions, derived from stored records (rebuildable).
- Explicit feedback with transparent, reversible effects.
- Auditable retention pruning: heavy text goes, metadata, hashes and lineage stay.
"""

from __future__ import annotations

import re
import zlib
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .db import Database, dumps, fts_query, loads, now_iso

NEAR_DUP_THRESHOLD = 0.7
FEEDBACK_VALUES = ("more", "less", "known", "useful", "wrong")
FEEDBACK_TARGETS = ("event", "cluster", "digest", "finding", "source")


# ---------------------------------------------------------------- similarity
def shingles(text: str, k: int = 4) -> set[int]:
    words = re.findall(r"\w+", text.lower())
    if len(words) < k:
        return {zlib.crc32(" ".join(words).encode())} if words else set()
    return {zlib.crc32(" ".join(words[i:i + k]).encode()) for i in range(len(words) - k + 1)}


def similarity(a: set[int], b: set[int]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def find_near_duplicate(db: Database, text: str, *, exclude_item_id: int | None = None, days: int = 30,
                        limit: int = 400, threshold: float = NEAR_DUP_THRESHOLD) -> int | None:
    """Most similar recent canonical item (not itself a duplicate), if similar enough."""
    if len(text) < 200:
        return None  # too short to judge: snippets and titles collide by accident
    since = (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()
    size = len(text)
    rows = db.all(
        """SELECT id, text FROM items WHERE text IS NOT NULL AND duplicate_of IS NULL AND near_duplicate_of IS NULL
           AND id <> ? AND last_seen_at >= ? AND length(text) BETWEEN ? AND ? ORDER BY id DESC LIMIT ?""",
        (exclude_item_id or -1, since, int(size * 0.6), int(size * 1.6) + 1, limit))
    mine = shingles(text)
    best, best_id = 0.0, None
    for r in rows:
        s = similarity(mine, shingles(r["text"]))
        if s > best:
            best, best_id = s, r["id"]
    return best_id if best >= threshold else None


# ---------------------------------------------------------------- prior knowledge
def prior_knowledge(db: Database, objective: str, *, exclude_run_id: int | None = None, limit: int = 8) -> list[dict]:
    """Findings from earlier finished research related to the objective (context, not evidence).

    Related = the finding's text matches the objective's words (FTS), or it belongs to a research
    whose objective shares significant words with this one.
    """
    words = sorted({w for w in re.findall(r"\w+", objective.lower()) if len(w) > 3})
    if not words:
        return []
    found: dict[int, dict] = {}
    base = """SELECT f.id, f.run_id, f.title, f.summary, f.confidence, r.objective FROM findings f
              JOIN runs r ON r.id = f.run_id WHERE f.kind='finding' AND f.superseded_by IS NULL
              AND r.kind='research' AND r.status IN ('completed','partial') AND f.run_id <> ?"""
    try:
        for row in db.all(f"""SELECT * FROM ({base}) x JOIN findings_fts ON findings_fts.rowid = x.id
                              WHERE findings_fts MATCH ? ORDER BY rank LIMIT ?""",
                          (exclude_run_id or -1, " OR ".join(f'"{w}"' for w in words[:12]), limit)):
            found[row["id"]] = dict(row)
    except Exception:  # FTS syntax edge cases must never break planning
        pass
    rank = {"high": 0, "medium": 1, "low": 2}
    for row in db.all(base + " ORDER BY f.run_id DESC, f.id DESC LIMIT 400", (exclude_run_id or -1,)):
        if len(found) >= limit:
            break
        shared = words_in(row["objective"]) & set(words)
        if row["id"] not in found and len(shared) >= max(1, min(2, len(words) // 2)):
            found[row["id"]] = dict(row)
    return sorted(found.values(), key=lambda f: (rank.get(f["confidence"], 3), -f["run_id"]))[:limit]


def words_in(text: str) -> set[str]:
    return {w for w in re.findall(r"\w+", text.lower()) if len(w) > 3}


# ---------------------------------------------------------------- reputation
KIND_WEIGHT = {"primary": 1.0, "secondary": 0.7, "community": 0.5, "commercial": 0.35, "unknown": 0.4}
AUTH_WEIGHT = {"high": 1.0, "medium": 0.6, "low": 0.3}


def source_reputation(db: Database) -> list[dict]:
    """Per-host dimensions from everything ROS has read. Rebuildable; never the only record."""
    from .connectors.feeds import host_of

    stats: dict[str, dict] = defaultdict(lambda: {"docs": 0, "kinds": defaultdict(int), "authority": [],
                                                  "conflicts": 0, "injection": 0, "verified": 0, "disputed": 0,
                                                  "claims": 0, "feedback": 0})
    for r in db.all("SELECT url, meta_json FROM items WHERE url IS NOT NULL"):
        host = host_of(r["url"])
        meta = loads(r["meta_json"], {}) or {}
        s = stats[host]
        s["docs"] += 1
        if "source_kind" in meta:
            s["kinds"][meta["source_kind"]] += 1
            s["authority"].append(AUTH_WEIGHT.get(meta.get("authority", ""), 0.5))
            s["conflicts"] += bool(meta.get("conflicts_of_interest"))
            s["injection"] += bool(meta.get("injection_suspected"))
    for r in db.all("SELECT i.url, c.status FROM claims c JOIN items i ON i.id=c.item_id WHERE i.url IS NOT NULL"):
        s = stats[host_of(r["url"])]
        s["claims"] += 1
        s["verified"] += r["status"] == "verified"
        s["disputed"] += r["status"] == "disputed"
    for r in db.all("SELECT target_id, value FROM feedback WHERE target_kind='source'"):
        stats[r["target_id"]]["feedback"] += {"more": 1, "useful": 1, "less": -1, "wrong": -2}.get(r["value"], 0)
    out = []
    for host, s in stats.items():
        assessed = sum(s["kinds"].values())
        primary = s["kinds"].get("primary", 0) / assessed if assessed else 0.0
        kind = max(s["kinds"], key=s["kinds"].get) if s["kinds"] else "unknown"
        authority = sum(s["authority"]) / len(s["authority"]) if s["authority"] else 0.5
        accuracy = (s["verified"] + 1) / (s["verified"] + s["disputed"] + 2)   # Laplace-smoothed
        rep = {"host": host, "docs": s["docs"], "kind": kind, "primary_share": round(primary, 2),
               "authority": round(authority, 2), "accuracy": round(accuracy, 2),
               "conflicts_of_interest": s["conflicts"], "injection_attempts": s["injection"],
               "claims": s["claims"], "verified": s["verified"], "disputed": s["disputed"],
               "feedback": s["feedback"]}
        rep["score"] = reputation_score(rep)
        out.append(rep)
    return sorted(out, key=lambda r: (-r["score"], -r["docs"], r["host"]))


def reputation_score(rep: dict) -> float:
    """Transparent combination used only for ordering; every dimension stays visible on its own."""
    score = (0.35 * rep["authority"] + 0.25 * rep["accuracy"] + 0.2 * KIND_WEIGHT.get(rep["kind"], 0.4)
             + 0.2 * rep["primary_share"])
    if rep["docs"]:
        score -= 0.1 * min(1.0, rep["conflicts_of_interest"] / rep["docs"])
    score -= 0.2 * min(1, rep["injection_attempts"])
    score += 0.05 * max(-2, min(2, rep["feedback"]))
    return round(max(0.0, min(1.0, score)), 3)


# ---------------------------------------------------------------- feedback
def record_feedback(db: Database, target_kind: str, target_id: str | int, value: str, note: str = "") -> int:
    if target_kind not in FEEDBACK_TARGETS:
        raise ValueError(f"objetivo no válido: {target_kind} ({', '.join(FEEDBACK_TARGETS)})")
    if value not in FEEDBACK_VALUES:
        raise ValueError(f"valor no válido: {value} ({', '.join(FEEDBACK_VALUES)})")
    if target_kind in ("event", "cluster", "digest", "finding"):
        table = {"event": "events", "cluster": "clusters", "digest": "digests", "finding": "findings"}[target_kind]
        if not db.one(f"SELECT 1 FROM {table} WHERE id=?", (int(target_id),)):
            raise ValueError(f"{target_kind} {target_id} no existe")
    return db.insert("feedback", {"ts": now_iso(), "target_kind": target_kind, "target_id": str(target_id),
                                  "value": value, "note": note or None})


def feedback_adjustment(db: Database, watch_id: int, labels: list[str], entities: list[str], host: str
                        ) -> tuple[float, list[str]]:
    """Importance multiplier from explicit feedback on similar past events and on the source host.

    Rule (inspectable and reversible by deleting feedback): each matching 'less'/'known' lowers by 15%,
    each 'more'/'useful' raises by 15%, bounded to [0.5, 1.5].
    """
    keys = {x.lower() for x in labels + entities if x}
    factor, reasons = 1.0, []
    rows = db.all("""SELECT f.value, e.labels_json, e.entities_json, e.id FROM feedback f JOIN events e
                     ON f.target_kind='event' AND e.id = CAST(f.target_id AS INTEGER) WHERE e.watch_id=?
                     ORDER BY f.id DESC LIMIT 200""", (watch_id,))
    for r in rows:
        other = {x.lower() for x in loads(r["labels_json"], []) + loads(r["entities_json"], [])}
        if keys & other:
            step = {"less": 0.85, "known": 0.85, "wrong": 0.85, "more": 1.15, "useful": 1.15}[r["value"]]
            factor *= step
            reasons.append(f"feedback '{r['value']}' en evento {r['id']} ({', '.join(sorted(keys & other))})")
    for r in db.all("SELECT value FROM feedback WHERE target_kind='source' AND target_id=?", (host,)):
        factor *= {"less": 0.85, "wrong": 0.8, "more": 1.15, "useful": 1.15}.get(r["value"], 1.0)
        reasons.append(f"feedback '{r['value']}' sobre la fuente {host}")
    return max(0.5, min(1.5, factor)), reasons


# ---------------------------------------------------------------- retention
def prune(db: Database, older_than_days: int, *, dry_run: bool = False) -> dict:
    """Drop the full text of old items (and their snapshots), keeping metadata, hashes, claims and links.

    Items still pending analysis are never pruned. Each real prune is recorded in `prune_log`.
    """
    if older_than_days < 1:
        raise ValueError("older_than_days debe ser >= 1")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).replace(microsecond=0).isoformat()
    where = """text IS NOT NULL AND last_seen_at < ? AND id NOT IN
               (SELECT item_id FROM watch_items WHERE analysis='pending')"""
    row = db.one(f"SELECT COUNT(*) n, COALESCE(SUM(length(text)),0) chars FROM items WHERE {where}", (cutoff,))
    snaps = db.one("""SELECT COUNT(*) n FROM snapshots WHERE text IS NOT NULL AND item_id IN
                      (SELECT id FROM items WHERE """ + where + ")", (cutoff,))["n"]
    result = {"items": row["n"], "snapshots": snaps, "chars": row["chars"], "cutoff": cutoff, "dry_run": dry_run}
    if dry_run or not row["n"]:
        return result
    ts = now_iso()
    with db.tx():
        db.execute("UPDATE snapshots SET text=NULL WHERE text IS NOT NULL AND item_id IN (SELECT id FROM items WHERE "
                   + where + ")", (cutoff,))
        db.execute(f"UPDATE items SET text=NULL, text_pruned_at=? WHERE {where}", (ts, cutoff))
        db.insert("prune_log", {"ts": ts, "criteria_json": dumps({"older_than_days": older_than_days, "cutoff": cutoff}),
                                "items": row["n"], "snapshots": snaps, "chars": row["chars"]})
    return result


def search_corpus(db: Database, text: str, limit: int = 20) -> dict[str, list[dict]]:
    """FTS over documents, claims, findings and watch events, with provenance."""
    q = fts_query(text)
    items = db.all("""SELECT i.id, i.title, i.url, i.published_at, snippet(items_fts, 1, '[', ']', '…', 12) snip
                      FROM items_fts JOIN items i ON i.id=items_fts.rowid WHERE items_fts MATCH ?
                      ORDER BY rank LIMIT ?""", (q, limit))
    claims = db.all("""SELECT c.id, c.run_id, c.claim_type, c.status, c.text, i.url FROM claims_fts
                       JOIN claims c ON c.id=claims_fts.rowid JOIN items i ON i.id=c.item_id
                       WHERE claims_fts MATCH ? ORDER BY rank LIMIT ?""", (q, limit))
    findings = db.all("""SELECT f.id, f.run_id, f.kind, f.confidence, f.title, f.summary FROM findings_fts
                         JOIN findings f ON f.id=findings_fts.rowid WHERE findings_fts MATCH ?
                         ORDER BY rank LIMIT ?""", (q, limit))
    events = db.all("""SELECT e.id, w.name watch, e.title, e.summary, e.importance, e.created_at FROM events_fts
                       JOIN events e ON e.id=events_fts.rowid JOIN watches w ON w.id=e.watch_id
                       WHERE events_fts MATCH ? ORDER BY rank LIMIT ?""", (q, limit))
    return {"items": [dict(r) for r in items], "claims": [dict(r) for r in claims],
            "findings": [dict(r) for r in findings], "events": [dict(r) for r in events]}
