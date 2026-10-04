"""Adaptive, bounded, resumable multi-round research.

Every stage is checkpointed in SQLite:

  plan -> [round: searching -> fetching -> extracting -> analyzing -> done]* -> report

A run interrupted at any point resumes from the last durable stage without
re-searching, re-fetching or re-extracting what is already stored.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from ..budget import Budget, Ledger
from ..connectors.base import NormalizedItem
from ..connectors.feeds import WebPageConnector, host_of
from ..db import dumps, loads, now_iso, text_hash
from ..errors import BudgetExhausted, ErrorKind, RosError
from ..registry import App
from ..security import canonical_url, host_in, wrap_untrusted
from . import prompts
from .report import render_report

TERMINAL = {"completed", "partial", "cancelled"}
MAX_PER_HOST_PER_ROUND = 2


def norm_query(text: str) -> str:
    words = re.findall(r"\w+", text.lower())
    return " ".join(sorted(set(words)))


class ResearchEngine:
    def __init__(self, app: App, *, echo: Callable[[str], None] = lambda s: None, sleep=time.sleep):
        self.app = app
        self.db = app.db
        self.echo = echo
        self.sleep = sleep

    # ------------------------------------------------------------------ setup
    def create(self, objective: str, budget: Budget | None = None, *, focus: str = "",
               exclude_domains: tuple[str, ...] = ()) -> int:
        budget = budget or self.app.settings.budget
        cfg = {"focus": focus, "exclude_domains": sorted(set(exclude_domains) | set(self.app.settings.exclude_domains)),
               "model": self.app.settings.model, "search_backend": self.app.search.name,
               "doc_chars": self.app.settings.doc_chars}
        ts = now_iso()
        run_id = self.db.insert("runs", {"kind": "research", "objective": objective.strip(), "status": "planned",
                                         "stage": "planning", "config_json": dumps(cfg),
                                         "budget_json": dumps(budget.to_dict()), "created_at": ts, "updated_at": ts})
        self.db.log(run_id, "created", "investigación creada", {"budget": budget.to_dict(), "config": cfg})
        return run_id

    def _set(self, run_id: int, **values) -> None:
        values["updated_at"] = now_iso()
        cols = ", ".join(f"{k}=?" for k in values)
        self.db.execute(f"UPDATE runs SET {cols} WHERE id=?", (*values.values(), run_id))

    # ------------------------------------------------------------------ main loop
    def run(self, run_id: int) -> str:
        run = self.db.one("SELECT * FROM runs WHERE id=?", (run_id,))
        if run is None:
            raise ValueError(f"run {run_id} not found")
        if run["status"] in TERMINAL:
            return run["status"]
        budget = Budget.from_dict(loads(run["budget_json"]))
        cfg = loads(run["config_json"])
        ledger = Ledger(self.db, run_id, budget)
        self._set(run_id, status="running")
        self.db.log(run_id, "resumed" if run["plan_json"] else "started", "ejecución iniciada")
        stop_reason, limit_hit = None, None
        try:
            if not run["plan_json"]:
                self._plan(run_id, run["objective"], cfg, ledger)
            while True:
                ledger.checkpoint_time()
                stop_reason = self._should_stop(run_id, budget)
                if stop_reason:
                    break
                rnd = self._current_round(run_id, budget)
                self._execute_round(run_id, rnd, cfg, budget, ledger)
        except BudgetExhausted as exc:
            limit_hit = exc.limit
            stop_reason = f"límite alcanzado: {exc.message}"
            self.db.record_error(run_id=run_id, kind=exc.kind.value, message=exc.message,
                                 impact="la investigación se detuvo antes de completar el plan")
            self.echo(f"⚠ {exc.message}")
        except KeyboardInterrupt:
            ledger.checkpoint_time()
            self._set(run_id, status="paused")
            self.db.log(run_id, "paused", "interrumpida por el usuario; se puede reanudar")
            raise
        except RosError as exc:
            ledger.checkpoint_time()
            self.db.record_error(run_id=run_id, kind=exc.kind.value, message=exc.message,
                                 impact="ejecución detenida; reanudable cuando se corrija la causa")
            self._set(run_id, status="failed", outcome_note=str(exc))
            self.echo(f"✖ {exc}")
            if exc.fatal or not self._has_material(run_id):
                return "failed"
            stop_reason = f"error: {exc.message}"
        self.db.log(run_id, "stop", stop_reason or "fin")
        return self._finalize(run_id, stop_reason or "", limit_hit, ledger)

    # ------------------------------------------------------------------ planning
    def _plan(self, run_id: int, objective: str, cfg: dict, ledger: Ledger) -> None:
        self._set(run_id, stage="planning")
        self.echo("· Planificando…")
        user = f"Research objective:\n{objective}\n"
        if cfg.get("focus"):
            user += f"\nThe user asks to pay special attention to:\n{cfg['focus']}\n"
        plan = self.app.llm.complete_json(purpose="plan", system=prompts.PLAN_SYSTEM, user=user,
                                          schema=prompts.PLAN_SCHEMA, max_tokens=prompts.MAX_TOKENS["plan"],
                                          ledger=ledger)
        for i, sq in enumerate(plan["subquestions"]):
            sq["id"] = sq.get("id") or f"q{i + 1}"
            sq["origin"] = "plan"
        self._save_plan(run_id, plan, round_n=0, reason="plan inicial")
        self.echo(f"  {plan['interpretation']}")
        for sq in plan["subquestions"]:
            self.echo(f"  - [{sq['id']}] {sq['question']}")

    def _save_plan(self, run_id: int, plan: dict, *, round_n: int, reason: str) -> None:
        with self.db.tx():
            row = self.db.one("SELECT plan_version FROM runs WHERE id=?", (run_id,))
            version = row["plan_version"] + 1
            self.db.insert("plan_versions", {"run_id": run_id, "version": version, "round_n": round_n,
                                             "plan_json": dumps(plan), "reason": reason, "created_at": now_iso()})
            self._set(run_id, plan_json=dumps(plan), plan_version=version)

    def _plan_of(self, run_id: int) -> dict:
        return loads(self.db.one("SELECT plan_json FROM runs WHERE id=?", (run_id,))["plan_json"])

    # ------------------------------------------------------------------ rounds
    def _rounds(self, run_id: int):
        return self.db.all("SELECT * FROM rounds WHERE run_id=? ORDER BY n", (run_id,))

    def _should_stop(self, run_id: int, budget: Budget) -> str | None:
        rounds = self._rounds(run_id)
        if not rounds or rounds[-1]["stage"] != "done":
            return None
        last = loads(rounds[-1]["analysis_json"], {})
        if len(rounds) >= budget.max_rounds:
            return f"máximo de rondas alcanzado ({budget.max_rounds})"
        if last.get("should_stop") and last.get("coverage", 0) >= 0.6:
            return f"cobertura suficiente ({last.get('coverage', 0):.0%}): {last.get('stop_reason', '')}"
        if not self._pending_next_queries(run_id, last):
            return "sin consultas nuevas que ejecutar"
        if len(rounds) >= 2:
            recent = [self._new_claims_in_round(run_id, r["n"]) for r in rounds[-2:]]
            if sum(recent) == 0:
                return "dos rondas seguidas sin información nueva"
        return None

    def _pending_next_queries(self, run_id: int, analysis: dict) -> list[dict]:
        done = {r["norm"] for r in self.db.all("SELECT norm FROM queries WHERE run_id=?", (run_id,))}
        out, seen = [], set()
        for q in analysis.get("next_queries", []):
            text = " ".join(q.get("text", "").split())[:200]
            n = norm_query(text)
            if not n or n in done or n in seen:
                continue
            seen.add(n)
            out.append({**q, "text": text})
        return out

    def _new_claims_in_round(self, run_id: int, n: int) -> int:
        return self.db.one("SELECT COUNT(*) c FROM claims WHERE run_id=? AND round_n=?", (run_id, n))["c"]

    def _current_round(self, run_id: int, budget: Budget):
        rounds = self._rounds(run_id)
        if rounds and rounds[-1]["stage"] != "done":
            return rounds[-1]
        n = len(rounds) + 1
        if n == 1:
            candidates = self._plan_of(run_id).get("queries", [])
        else:
            candidates = self._pending_next_queries(run_id, loads(rounds[-1]["analysis_json"], {}))
        with self.db.tx():
            self.db.insert("rounds", {"run_id": run_id, "n": n, "stage": "searching", "started_at": now_iso()})
            added = 0
            for q in candidates:
                if added >= budget.queries_per_round:
                    break
                text = " ".join(q.get("text", "").split())[:200]
                norm = norm_query(text)
                if not norm:
                    continue
                cur = self.db.execute("INSERT OR IGNORE INTO queries (run_id, round_n, text, norm, rationale) VALUES (?,?,?,?,?)",
                                      (run_id, n, text, norm, q.get("rationale", "")))
                if cur.rowcount:
                    added += 1
                else:
                    self.db.log(run_id, "query_skipped", f"consulta repetida descartada: {text}")
        self._set(run_id, stage=f"round {n}")
        self.echo(f"\n━━ Ronda {n} ━━")
        return self.db.one("SELECT * FROM rounds WHERE run_id=? AND n=?", (run_id, n))

    def _set_round(self, run_id: int, n: int, **values) -> None:
        cols = ", ".join(f"{k}=?" for k in values)
        self.db.execute(f"UPDATE rounds SET {cols} WHERE run_id=? AND n=?", (*values.values(), run_id, n))

    def _execute_round(self, run_id: int, rnd, cfg: dict, budget: Budget, ledger: Ledger) -> None:
        n = rnd["n"]
        stage = rnd["stage"]
        if stage == "searching":
            self._search(run_id, n, cfg, budget, ledger)
            self._set_round(run_id, n, stage="fetching")
            stage = "fetching"
        if stage == "fetching":
            self._fetch(run_id, n, budget, ledger)
            self._set_round(run_id, n, stage="extracting")
            stage = "extracting"
        if stage == "extracting":
            self._extract(run_id, n, cfg, ledger)
            self._set_round(run_id, n, stage="analyzing")
            stage = "analyzing"
        if stage == "analyzing":
            self._analyze(run_id, n, budget, ledger)

    # ------------------------------------------------------------------ search
    def _search(self, run_id: int, n: int, cfg: dict, budget: Budget, ledger: Ledger) -> None:
        excluded = cfg.get("exclude_domains", [])
        for q in self.db.all("SELECT * FROM queries WHERE run_id=? AND round_n=? AND status='pending'", (run_id, n)):
            ledger.check_time()
            self.echo(f"  🔎 {q['text']}")
            try:
                hits = self._with_retries(lambda: self.app.search.search(q["text"], budget.results_per_query), budget)
            except RosError as exc:
                self.db.record_error(run_id=run_id, kind=exc.kind.value, subject=f"búsqueda: {q['text']}",
                                     message=exc.message, impact="consulta sin resultados")
                self.db.execute("UPDATE queries SET status='failed', results=0 WHERE id=?", (q["id"],))
                if exc.kind in (ErrorKind.INVALID_CREDENTIALS, ErrorKind.PLATFORM_BLOCKED):
                    raise
                continue
            ledger.record("search", purpose=self.app.search.name)
            with self.db.tx():
                for hit in hits:
                    try:
                        cid = canonical_url(hit.url)
                    except ValueError:
                        continue
                    host = host_of(hit.url)
                    status, reason = "candidate", None
                    if not hit.url.startswith(("http://", "https://")):
                        status, reason = "rejected", "esquema no permitido"
                    elif host_in(host, excluded):
                        status, reason = "rejected", "dominio excluido"
                    self.db.execute(
                        """INSERT OR IGNORE INTO run_items (run_id, round_n, url, canonical_url, title, snippet, query_id, status, reason)
                           VALUES (?,?,?,?,?,?,?,?,?)""",
                        (run_id, n, hit.url, cid, hit.title, hit.snippet, q["id"], status, reason))
                self.db.execute("UPDATE queries SET status='done', results=? WHERE id=?", (len(hits), q["id"]))

    def _with_retries(self, fn, budget: Budget):
        attempt = 0
        while True:
            try:
                return fn()
            except RosError as exc:
                if not exc.retryable or attempt >= budget.retries:
                    raise
                attempt += 1
                self.sleep(min(exc.retry_after or 2 ** attempt, 30))

    # ------------------------------------------------------------------ fetch
    def _select(self, run_id: int, n: int, budget: Budget, ledger: Ledger) -> list:
        remaining = budget.max_sources - ledger.fetched_sources()
        if remaining <= 0:
            raise BudgetExhausted("sources", f"máximo de fuentes alcanzado ({budget.max_sources})")
        rows = self.db.all("SELECT * FROM run_items WHERE run_id=? AND status='candidate' ORDER BY round_n DESC, id",
                           (run_id,))
        per_host = Counter(host_of(r["url"]) for r in self.db.all(
            "SELECT url FROM run_items WHERE run_id=? AND status IN ('extracted','irrelevant','fetched','reused')", (run_id,)))
        chosen, round_hosts = [], Counter()
        # Diversity first: at most N per host per round, prefer hosts not yet used in this run.
        rows = sorted(rows, key=lambda r: (per_host[host_of(r["url"])], r["round_n"] != n, r["id"]))
        for r in rows:
            h = host_of(r["url"])
            if round_hosts[h] >= MAX_PER_HOST_PER_ROUND:
                continue
            chosen.append(r)
            round_hosts[h] += 1
            if len(chosen) >= remaining:
                break
        return chosen

    def _fetch(self, run_id: int, n: int, budget: Budget, ledger: Ledger) -> None:
        selected = self.db.all("SELECT * FROM run_items WHERE run_id=? AND status='selected'", (run_id,))
        if not selected:
            chosen = self._select(run_id, n, budget, ledger)
            with self.db.tx():
                for r in chosen:
                    self.db.execute("UPDATE run_items SET status='selected', round_n=? WHERE id=?", (n, r["id"]))
            selected = self.db.all("SELECT * FROM run_items WHERE run_id=? AND status='selected'", (run_id,))
        if not selected:
            self.echo("  (sin fuentes nuevas que descargar)")
            return
        # Knowledge reuse: a document already stored (from any earlier run) is not downloaded again.
        to_fetch = []
        for r in selected:
            existing = self.db.one("SELECT id FROM items WHERE canonical_url=? AND text IS NOT NULL AND duplicate_of IS NULL "
                                   "ORDER BY last_seen_at DESC", (r["canonical_url"],))
            if existing:
                self.db.execute("UPDATE run_items SET status='reused', item_id=? WHERE id=?", (existing["id"], r["id"]))
                self.echo(f"  ♻ {r['url']}")
            else:
                to_fetch.append(r)
        ledger.check_time()
        web = WebPageConnector(self.app.fetcher)

        def job(r):
            try:
                return r, self._with_retries(lambda: web.fetch(r["url"]), budget), None
            except RosError as exc:
                return r, None, exc
            except Exception as exc:  # parser bugs must not kill the run
                return r, None, RosError(ErrorKind.INTERNAL, f"{type(exc).__name__}: {exc}")

        with ThreadPoolExecutor(max_workers=max(1, budget.concurrency)) as pool:
            for r, doc, err in pool.map(job, to_fetch):
                if err:
                    self.db.execute("UPDATE run_items SET status='failed', reason=? WHERE id=?", (str(err), r["id"]))
                    self.db.record_error(run_id=run_id, kind=err.kind.value, subject=r["url"], message=err.message,
                                         impact="fuente perdida; se continúa con las demás")
                    self.echo(f"  ✖ {r['url']} — {err.kind.value}")
                    continue
                ledger.record("fetch", bytes=doc.meta.get("bytes", 0))
                item_id, dup = self._store_document(doc)
                status = "fetched"
                if dup:
                    self.db.log(run_id, "duplicate", f"contenido idéntico a item {dup}: {r['url']}")
                self.db.execute("UPDATE run_items SET status=?, item_id=?, reason=? WHERE id=?",
                                (status, item_id, f"duplicado de item {dup}" if dup else None, r["id"]))
                self.echo(f"  ✔ {doc.title[:80]}  ({host_of(doc.url)})")
        ledger.checkpoint_time()

    def _store_document(self, doc: NormalizedItem) -> tuple[int, int | None]:
        """Store a fetched page as an item of its site source; detect exact duplicates by content hash."""
        h = text_hash(doc.text)
        ts = now_iso()
        with self.db.tx():
            source_id = self.db.upsert_source("site", host_of(doc.url), "public_web")
            dup = self.db.one("SELECT id FROM items WHERE content_hash=? AND duplicate_of IS NULL AND external_id<>?",
                              (h, doc.external_id))
            existing = self.db.one("SELECT id, content_hash FROM items WHERE source_id=? AND external_id=?",
                                   (source_id, doc.external_id))
            if existing:
                item_id = existing["id"]
                self.db.execute("UPDATE items SET text=?, title=?, content_hash=?, last_seen_at=? WHERE id=?",
                                (doc.text, doc.title, h, ts, item_id))
            else:
                item_id = self.db.insert("items", {
                    "source_id": source_id, "external_id": doc.external_id, "url": doc.url,
                    "canonical_url": canonical_url(doc.url), "title": doc.title, "author": doc.author,
                    "published_at": doc.published_at, "first_seen_at": ts, "last_seen_at": ts, "content_hash": h,
                    "text": doc.text, "meta_json": dumps(doc.meta), "duplicate_of": dup["id"] if dup else None})
            self.db.execute("INSERT OR IGNORE INTO snapshots (item_id, content_hash, observed_at) VALUES (?,?,?)",
                            (item_id, h, ts))
        return item_id, (dup["id"] if dup else None)

    # ------------------------------------------------------------------ extract
    def _extract(self, run_id: int, n: int, cfg: dict, ledger: Ledger) -> None:
        run = self.db.one("SELECT objective FROM runs WHERE id=?", (run_id,))
        plan = self._plan_of(run_id)
        sq_text = "\n".join(f"[{s['id']}] {s['question']}" for s in plan.get("subquestions", []))
        rows = self.db.all(
            """SELECT ri.id rid, ri.url, i.id item_id, i.title, i.text, i.duplicate_of FROM run_items ri
               JOIN items i ON i.id = ri.item_id WHERE ri.run_id=? AND ri.status IN ('fetched','reused')""", (run_id,))
        seen_hashes = set()
        for r in rows:
            if r["duplicate_of"] or r["item_id"] in seen_hashes:
                # Exact duplicate content: keep provenance, skip the expensive analysis.
                self.db.execute("UPDATE run_items SET status='irrelevant', reason='duplicado exacto' WHERE id=?", (r["rid"],))
                continue
            seen_hashes.add(r["item_id"])
            already = self.db.one("SELECT COUNT(*) c FROM claims WHERE run_id=? AND item_id=?", (run_id, r["item_id"]))["c"]
            if already:
                self.db.execute("UPDATE run_items SET status='extracted' WHERE id=?", (r["rid"],))
                continue
            wrapped, truncated = wrap_untrusted(f"TITLE: {r['title']}\nURL: {r['url']}\n\n{r['text']}",
                                                label=r["url"], max_chars=cfg.get("doc_chars", 12000))
            user = (f"Research objective:\n{run['objective']}\n\nSub-questions:\n{sq_text}\n\n"
                    f"Document to analyse:\n{wrapped}")
            try:
                data = self.app.llm.complete_json(purpose="extract", system=prompts.EXTRACT_SYSTEM, user=user,
                                                  schema=prompts.EXTRACT_SCHEMA, max_tokens=prompts.MAX_TOKENS["extract"],
                                                  ledger=ledger)
            except RosError as exc:
                # Budget exhaustion stops the round; the document stays 'fetched' (pending), not failed.
                if exc.fatal or isinstance(exc, BudgetExhausted):
                    raise
                self.db.record_error(run_id=run_id, kind=exc.kind.value, subject=r["url"], message=exc.message,
                                     impact="documento descargado pero no analizado")
                self.db.execute("UPDATE run_items SET status='failed', reason=? WHERE id=?", (str(exc), r["rid"]))
                continue
            if truncated:
                self.db.record_error(run_id=run_id, kind=ErrorKind.EXTRACTION_INCOMPLETE.value, subject=r["url"],
                                     message=f"documento recortado a {cfg.get('doc_chars', 12000)} caracteres",
                                     impact="parte del documento no fue analizada")
            if data.get("injection_suspected"):
                self.db.log(run_id, "injection", f"posible prompt injection en {r['url']}; tratado como dato")
                self.echo(f"  ⚠ posible prompt injection en {r['url']} (ignorado)")
            meta = {"source_kind": data["source_kind"], "authority": data["authority"],
                    "conflicts_of_interest": data["conflicts_of_interest"], "summary": data["summary"],
                    "entities": data["entities"], "new_terms": data["new_terms"],
                    "injection_suspected": data["injection_suspected"]}
            with self.db.tx():
                for c in data["claims"] if data["relevant"] else []:
                    if not c["text"].strip():
                        continue
                    self.db.insert("claims", {
                        "run_id": run_id, "item_id": r["item_id"], "round_n": n, "text": c["text"].strip()[:1000],
                        "claim_type": c["type"], "quote": c["quote"][:400], "subtopic": c["subquestion_id"],
                        "confidence": max(0.0, min(1.0, float(c["confidence"]))), "created_at": now_iso()})
                self.db.execute("UPDATE items SET meta_json=? WHERE id=?", (dumps(meta), r["item_id"]))
                self.db.execute("UPDATE run_items SET status=? WHERE id=?",
                                ("extracted" if data["relevant"] else "irrelevant", r["rid"]))
            self.echo(f"  ✎ {len(data['claims']) if data['relevant'] else 0} afirmaciones · {r['title'][:60]}")

    # ------------------------------------------------------------------ analyze
    def _claims_context(self, run_id: int) -> tuple[str, dict[int, str]]:
        rows = self.db.all(
            """SELECT c.id, c.claim_type, c.text, c.subtopic, i.url, i.meta_json FROM claims c
               JOIN items i ON i.id=c.item_id WHERE c.run_id=? ORDER BY c.id""", (run_id,))
        hosts, lines = {}, []
        for r in rows:
            meta = loads(r["meta_json"], {}) or {}
            hosts[r["id"]] = host_of(r["url"])
            lines.append(f"C{r['id']} [{r['claim_type']}] ({hosts[r['id']]}, {meta.get('source_kind', '?')}"
                         f"{', sq=' + r['subtopic'] if r['subtopic'] else ''}): {r['text']}")
        return "\n".join(lines), hosts

    def _analyze(self, run_id: int, n: int, budget: Budget, ledger: Ledger) -> None:
        run = self.db.one("SELECT objective, config_json FROM runs WHERE id=?", (run_id,))
        plan = self._plan_of(run_id)
        claims_txt, hosts = self._claims_context(run_id)
        queries = [r["text"] for r in self.db.all("SELECT text FROM queries WHERE run_id=?", (run_id,))]
        sq_text = "\n".join(f"[{s['id']}] {s['question']}" for s in plan["subquestions"])
        remaining_rounds = budget.max_rounds - n
        user = (f"Objective:\n{run['objective']}\n\nSub-questions:\n{sq_text}\n\n"
                f"Queries already executed:\n" + "\n".join(f"- {q}" for q in queries) +
                f"\n\nRound just completed: {n}. Remaining rounds allowed: {remaining_rounds}. "
                f"Propose at most {budget.queries_per_round} next queries.\n\n"
                f"Claims gathered so far (untrusted data):\n<claims>\n{claims_txt or '(none)'}\n</claims>")
        self.echo("  ⚙ analizando la ronda…")
        analysis = self.app.llm.complete_json(purpose="analyze", system=prompts.ANALYZE_SYSTEM, user=user,
                                              schema=prompts.ANALYZE_SCHEMA, max_tokens=prompts.MAX_TOKENS["analyze"],
                                              ledger=ledger)
        valid_ids = set(hosts)
        ts = now_iso()
        with self.db.tx():
            for f in analysis["findings"]:
                ids = [i for i in f["claim_ids"] if i in valid_ids]
                self.db.insert("findings", {"run_id": run_id, "round_n": n, "title": f["title"], "summary": f["summary"],
                                            "confidence": f["confidence"], "claim_ids_json": dumps(ids),
                                            "kind": "finding", "created_at": ts})
                # Deterministic verification rule: high confidence + ≥2 independent hosts.
                if f["confidence"] == "high" and len({hosts[i] for i in ids}) >= 2:
                    self.db.execute(f"UPDATE claims SET status='verified' WHERE status='unverified' AND claim_type='fact' "
                                    f"AND id IN ({','.join('?' * len(ids))})", ids)
            for c in analysis["contradictions"]:
                ids = [i for i in c["claim_ids"] if i in valid_ids]
                self.db.insert("findings", {"run_id": run_id, "round_n": n, "title": "Contradicción",
                                            "summary": c["description"], "confidence": "low",
                                            "claim_ids_json": dumps(ids), "kind": "contradiction", "created_at": ts})
                if ids:
                    self.db.execute(f"UPDATE claims SET status='disputed' WHERE id IN ({','.join('?' * len(ids))})", ids)
            for g in analysis["gaps"]:
                self.db.insert("findings", {"run_id": run_id, "round_n": n, "title": "Vacío", "summary": g,
                                            "confidence": "low", "claim_ids_json": "[]", "kind": "gap", "created_at": ts})
            self._set_round(run_id, n, stage="done", analysis_json=dumps(analysis), finished_at=ts)
        self._adapt_plan(run_id, n, plan, analysis, budget)
        self.echo(f"  → cobertura {analysis['coverage']:.0%}, {len(analysis['findings'])} hallazgos, "
                  f"{len(analysis['contradictions'])} contradicciones, {len(analysis['next_queries'])} consultas propuestas")

    def _adapt_plan(self, run_id: int, n: int, plan: dict, analysis: dict, budget: Budget) -> None:
        """Bounded plan update: discovered subtopics join the plan up to the depth limit."""
        changed, reasons = False, []
        status = {s["id"]: s for s in analysis["subquestion_status"]}
        for sq in plan["subquestions"]:
            if sq["id"] in status:
                new = status[sq["id"]]["status"]
                if sq.get("status") != new:
                    sq["status"], changed = new, True
        discovered = [s for s in plan["subquestions"] if s.get("origin") == "discovery"]
        existing = {norm_query(s["question"]) for s in plan["subquestions"]}
        for sub in sorted(analysis["new_subtopics"], key=lambda s: -s["relevance"]):
            if len(discovered) >= budget.max_subtopics:
                self.db.log(run_id, "subtopic_capped", f"subtema no añadido (límite de profundidad): {sub['question']}")
                continue
            if sub["relevance"] < 0.5 or norm_query(sub["question"]) in existing:
                continue
            new_sq = {"id": f"d{n}_{len(discovered) + 1}", "question": sub["question"], "priority": "medium",
                      "origin": "discovery", "reason": sub["reason"], "round": n}
            plan["subquestions"].append(new_sq)
            discovered.append(new_sq)
            existing.add(norm_query(sub["question"]))
            reasons.append(f"nuevo subtema: {sub['question']}")
            self.db.insert("findings", {"run_id": run_id, "round_n": n, "title": sub["question"], "summary": sub["reason"],
                                        "confidence": "low", "claim_ids_json": "[]", "kind": "discovery",
                                        "created_at": now_iso()})
            self.echo(f"  ✚ subtema descubierto: {sub['question']}")
            changed = True
        for line in analysis["drop_lines"]:
            self.db.log(run_id, "drop_line", f"línea abandonada: {line}")
        if changed:
            self._save_plan(run_id, plan, round_n=n, reason="; ".join(reasons) or "estado de sub-preguntas actualizado")

    # ------------------------------------------------------------------ finalize
    def _has_material(self, run_id: int) -> bool:
        return bool(self.db.one("SELECT 1 FROM claims WHERE run_id=? LIMIT 1", (run_id,)))

    def _finalize(self, run_id: int, stop_reason: str, limit_hit: str | None, ledger: Ledger) -> str:
        self._set(run_id, stage="reporting")
        ledger.enter_final_phase()
        run = self.db.one("SELECT * FROM runs WHERE id=?", (run_id,))
        synthesis, synth_error = None, None
        if self._has_material(run_id):
            self.echo("\n· Redactando informe…")
            try:
                synthesis = self._synthesize(run_id, run["objective"], ledger)
            except RosError as exc:
                synth_error = exc
                self.db.record_error(run_id=run_id, kind=exc.kind.value, message=exc.message,
                                     impact="informe generado sin síntesis del modelo (solo hallazgos)")
        total = self.db.one("SELECT COUNT(*) c FROM run_items WHERE run_id=? AND status NOT IN ('candidate','rejected')",
                            (run_id,))["c"]
        failed = self.db.one("SELECT COUNT(*) c FROM run_items WHERE run_id=? AND status='failed'", (run_id,))["c"]
        degraded = []
        if limit_hit:
            degraded.append(f"se alcanzó el límite de {limit_hit}")
        if total and failed / total > 0.4:
            degraded.append(f"{failed} de {total} fuentes fallaron")
        if synth_error:
            degraded.append("la síntesis final falló")
        if not self._has_material(run_id):
            degraded.append("no se obtuvo ninguna evidencia")
        if run["status"] == "failed":
            degraded.append(run["outcome_note"] or "error durante la ejecución")
        status = "partial" if degraded else "completed"
        if not self._has_material(run_id) and not synthesis:
            status = "partial" if total else "failed"
        ledger.checkpoint_time()
        md = render_report(self.db, run_id, synthesis=synthesis, stop_reason=stop_reason, degraded=degraded,
                           status=status, usage=ledger.summary())
        self._set(run_id, status=status, stage="done", report_md=md, finished_at=now_iso(),
                  outcome_note="; ".join(degraded) or stop_reason)
        self.db.log(run_id, "finished", status, {"degraded": degraded, "stop_reason": stop_reason})
        return status

    def _synthesize(self, run_id: int, objective: str, ledger: Ledger) -> dict:
        claims_txt, _ = self._claims_context(run_id)
        last_round = self.db.one("SELECT MAX(round_n) m FROM findings WHERE run_id=?", (run_id,))["m"]
        findings = self.db.all("SELECT * FROM findings WHERE run_id=? AND round_n=? ORDER BY id", (run_id, last_round))
        ftxt = "\n".join(f"- ({f['kind']}, {f['confidence']}) {f['title']}: {f['summary']} {loads(f['claim_ids_json'])}"
                         for f in findings)
        discoveries = self.db.all("SELECT title, summary FROM findings WHERE run_id=? AND kind='discovery'", (run_id,))
        dtxt = "\n".join(f"- {d['title']}: {d['summary']}" for d in discoveries)
        errors = self.db.all("SELECT kind, COUNT(*) c FROM errors WHERE run_id=? GROUP BY kind", (run_id,))
        etxt = ", ".join(f"{e['kind']}×{e['c']}" for e in errors) or "none"
        user = (f"Objective:\n{objective}\n\nLatest findings, contradictions and gaps:\n{ftxt or '(none)'}\n\n"
                f"Discovered subtopics:\n{dtxt or '(none)'}\n\nCollection errors: {etxt}\n\n"
                f"All claims (untrusted data):\n<claims>\n{claims_txt}\n</claims>")
        return self.app.llm.complete_json(purpose="synthesize", system=prompts.SYNTH_SYSTEM, user=user,
                                          schema=prompts.SYNTH_SCHEMA, max_tokens=prompts.MAX_TOKENS["synthesize"],
                                          ledger=ledger)
