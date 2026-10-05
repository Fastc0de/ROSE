"""Budgets and the usage ledger.

Limits are checked *before* spending (conservative reservation of the worst case)
and recorded from actual usage afterwards. Elapsed time survives restarts:
persisted segments plus the active monotonic segment.
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, fields

from .db import Database, now_iso
from .errors import BudgetExhausted


@dataclass
class Budget:
    max_rounds: int = 4
    max_sources: int = 30
    max_minutes: float = 20.0
    max_cost_usd: float = 1.50
    max_tokens: int = 400_000
    queries_per_round: int = 4
    results_per_query: int = 6
    max_subtopics: int = 6          # depth: how many discovered subtopics may join the plan
    concurrency: int = 4
    retries: int = 2
    final_reserve: float = 0.30     # fraction of cost/tokens kept for the final report (must cover its worst case)

    @classmethod
    def from_dict(cls, data: dict) -> "Budget":
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown budget keys: {sorted(unknown)}")
        return cls(**data)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Totals:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    llm_calls: int = 0
    fetches: int = 0
    searches: int = 0
    bytes: int = 0

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class Ledger:
    """Run-scoped usage accounting backed by the `usage` table."""

    def __init__(self, db: Database, run_id: int | None, budget: Budget):
        self.db = db
        self.run_id = run_id
        self.budget = budget
        self._segment_start = time.monotonic()
        self._lock = threading.Lock()
        self._reserved_cost = 0.0
        self._reserved_tokens = 0
        self._final_phase = False

    # -- time ---------------------------------------------------------------
    def elapsed_s(self) -> float:
        base = 0.0
        if self.run_id is not None:
            row = self.db.one("SELECT elapsed_s FROM runs WHERE id=?", (self.run_id,))
            base = row["elapsed_s"] if row else 0.0
        return base + (time.monotonic() - self._segment_start)

    def checkpoint_time(self) -> None:
        """Persist the active segment so elapsed time survives a crash/restart."""
        if self.run_id is None:
            return
        now = time.monotonic()
        delta = now - self._segment_start
        self._segment_start = now
        self.db.execute("UPDATE runs SET elapsed_s = elapsed_s + ?, updated_at=? WHERE id=?",
                        (delta, now_iso(), self.run_id))

    # -- totals -------------------------------------------------------------
    def totals(self) -> Totals:
        if self.run_id is None:
            return Totals()
        row = self.db.one(
            """SELECT COALESCE(SUM(input_tokens),0) it, COALESCE(SUM(output_tokens),0) ot,
                      COALESCE(SUM(cost_usd),0) c, COALESCE(SUM(bytes),0) b,
                      SUM(kind='llm') llm, SUM(kind='fetch') f, SUM(kind='search') s
               FROM usage WHERE run_id=?""", (self.run_id,))
        return Totals(row["it"], row["ot"], row["c"], row["llm"] or 0, row["f"] or 0, row["s"] or 0, row["b"])

    def fetched_sources(self) -> int:
        if self.run_id is None:
            return 0
        row = self.db.one("SELECT COUNT(*) n FROM run_items WHERE run_id=? AND status IN ('fetched','reused','failed','extracted','irrelevant')",
                          (self.run_id,))
        return int(row["n"])

    # -- enforcement --------------------------------------------------------
    def enter_final_phase(self) -> None:
        """Allow spending the reserve kept for the final report."""
        self._final_phase = True

    def _caps(self) -> tuple[float, int]:
        factor = 1.0 if self._final_phase else (1.0 - self.budget.final_reserve)
        return self.budget.max_cost_usd * factor, int(self.budget.max_tokens * factor)

    def check_time(self) -> None:
        limit = self.budget.max_minutes * 60
        if not self._final_phase and self.elapsed_s() >= limit:
            raise BudgetExhausted("time", f"tiempo máximo alcanzado ({self.budget.max_minutes:g} min)")

    def check_sources(self, extra: int = 1) -> None:
        if self.fetched_sources() + extra > self.budget.max_sources:
            raise BudgetExhausted("sources", f"máximo de fuentes alcanzado ({self.budget.max_sources})")

    def reserve_llm(self, est_input_tokens: int, max_output_tokens: int, est_cost: float) -> None:
        """Reject a model call whose worst case would cross a hard cap."""
        self.check_time()
        cost_cap, token_cap = self._caps()
        with self._lock:
            t = self.totals()
            if t.tokens + self._reserved_tokens + est_input_tokens + max_output_tokens > token_cap:
                raise BudgetExhausted("tokens", f"límite de tokens alcanzado ({t.tokens}/{self.budget.max_tokens})")
            if t.cost_usd + self._reserved_cost + est_cost > cost_cap:
                raise BudgetExhausted("cost", f"límite de coste alcanzado (${t.cost_usd:.4f}/${self.budget.max_cost_usd:.2f})")
            self._reserved_tokens += est_input_tokens + max_output_tokens
            self._reserved_cost += est_cost

    def grant_output(self, est_input_tokens: int, max_output_tokens: int, min_output_tokens: int,
                     in_price: float, out_price: float) -> tuple[int, float]:
        """Reserve the largest output ceiling (<= max) that every cap can still pay for.

        The ceiling is sent to the model as max_tokens, so the reservation stays a true worst case.
        Raises BudgetExhausted when even `min_output_tokens` no longer fits.
        Returns (granted_output_tokens, reserved_cost).
        """
        self.check_time()
        cost_cap, token_cap = self._caps()
        with self._lock:
            t = self.totals()
            input_cost = est_input_tokens * in_price / 1_000_000
            free_tokens = token_cap - t.tokens - self._reserved_tokens - est_input_tokens
            free_cost = cost_cap - t.cost_usd - self._reserved_cost - input_cost
            by_cost = int(free_cost * 1_000_000 / out_price) if out_price else max_output_tokens
            granted = min(max_output_tokens, free_tokens, by_cost)
            if granted < min_output_tokens:
                if free_tokens <= by_cost:
                    raise BudgetExhausted("tokens", f"límite de tokens alcanzado ({t.tokens}/{self.budget.max_tokens})")
                raise BudgetExhausted("cost", f"límite de coste alcanzado (${t.cost_usd:.4f}/${self.budget.max_cost_usd:.2f})")
            cost = input_cost + granted * out_price / 1_000_000
            self._reserved_tokens += est_input_tokens + granted
            self._reserved_cost += cost
            return granted, cost

    def settle_llm(self, est_input_tokens: int, max_output_tokens: int, est_cost: float, *, purpose: str,
                   model: str, input_tokens: int, output_tokens: int, cost: float) -> None:
        with self._lock:
            self._reserved_tokens -= est_input_tokens + max_output_tokens
            self._reserved_cost -= est_cost
            if input_tokens or output_tokens:
                self.record("llm", purpose=purpose, model=model, input_tokens=input_tokens,
                            output_tokens=output_tokens, cost_usd=cost)

    def record(self, kind: str, *, purpose: str | None = None, model: str | None = None, input_tokens: int = 0,
               output_tokens: int = 0, cost_usd: float = 0.0, bytes: int = 0) -> None:
        self.db.insert("usage", {"run_id": self.run_id, "ts": now_iso(), "kind": kind, "purpose": purpose,
                                 "model": model, "input_tokens": input_tokens, "output_tokens": output_tokens,
                                 "cost_usd": cost_usd, "bytes": bytes})

    def summary(self) -> dict:
        t = self.totals()
        return {
            "elapsed_min": round(self.elapsed_s() / 60, 2),
            "cost_usd": round(t.cost_usd, 4),
            "input_tokens": t.input_tokens,
            "output_tokens": t.output_tokens,
            "llm_calls": t.llm_calls,
            "searches": t.searches,
            "fetches": t.fetches,
            "sources": self.fetched_sources(),
            "bytes": t.bytes,
        }
