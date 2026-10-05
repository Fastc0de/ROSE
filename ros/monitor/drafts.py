"""Natural-language configuration: text -> reviewable ConfigurationDraft -> validated config.

The model only proposes values. Code validates them into a WatchSpec (or research settings),
adds deterministic warnings (missing credentials, unsupported platforms, cost) and lists what
requires the user's decision. Nothing recurring is ever enabled from a draft without explicit
confirmation by the caller.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from pydantic import ValidationError

from ..budget import Ledger
from ..db import dumps, now_iso
from ..errors import RosError
from ..llm import BOOL, INT, NUM, STR, arr, enum, obj
from ..registry import App
from .schedule import iso
from .spec import AlertRules, DigestPolicy, SourceRef, WatchSpec

CONFIGURE_SYSTEM = """You are the configuration stage of ROS, a research and monitoring system.
Convert the user's natural-language instruction into configuration values. Do not invent sources:
only list sources the user actually gave (URLs, subreddits like r/name, YouTube channels/handles, feeds,
search queries). Source kinds: rss, web, youtube, reddit, search, youtube_api, reddit_api, x, instagram,
facebook. If the user refers to sources they did not provide ("these accounts"), leave sources empty
and say so in unresolved. Decide kind: "research" for a one-off investigation, "watch" for following
sources or topics over time. every_minutes is the collection frequency. Digest: when to deliver the
consolidated report (daily at HH:MM, weekly, interval, threshold of events, or manual). Alerts: immediate
notification rules. Use defaults when the user says nothing, and list every assumption you made.
If several materially different interpretations exist, list them in interpretations.
Write human-readable fields in the user's language."""

CONFIGURE_SCHEMA = obj({
    "kind": enum("watch", "research"),
    "name": STR,
    "objective": STR,
    "focus": STR,
    "exclude_domains": arr(STR),
    "sources": arr(obj({"kind": STR, "locator": STR, "label": STR})),
    "topics": arr(STR),
    "exclusions": arr(STR),
    "every_minutes": INT,
    "digest_mode": enum("daily", "weekly", "interval", "threshold", "manual"),
    "digest_at": STR,
    "digest_weekday": INT,
    "digest_every_hours": NUM,
    "digest_min_events": INT,
    "alert_enabled": BOOL,
    "alert_min_importance": NUM,
    "alert_min_independent_sources": INT,
    "alert_keywords": arr(STR),
    "depth": enum("light", "standard", "deep"),
    "max_cost_usd": NUM,
    "duration_days": INT,
    "assumptions": arr(STR),
    "unresolved": arr(STR),
    "interpretations": arr(STR),
})

DEPTH_ROUNDS = {"light": 2, "standard": 4, "deep": 6}
NATIVE_PLATFORMS = {"x", "instagram", "facebook", "youtube_api", "reddit_api"}


@dataclass
class ConfigurationDraft:
    kind: str
    input_text: str
    spec: WatchSpec | None = None
    research: dict | None = None
    assumptions: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    requires_user_action: list[str] = field(default_factory=list)
    interpretations: list[str] = field(default_factory=list)
    draft_id: int | None = None

    @property
    def complete(self) -> bool:
        return (self.spec is not None or self.research is not None) and not self.unresolved

    def to_dict(self) -> dict:
        return {"kind": self.kind, "input_text": self.input_text,
                "spec": self.spec.model_dump() if self.spec else None, "research": self.research,
                "assumptions": self.assumptions, "unresolved": self.unresolved, "warnings": self.warnings,
                "requires_user_action": self.requires_user_action, "interpretations": self.interpretations}

    def render(self) -> str:
        lines = [f"Borrador de configuración ({'seguimiento' if self.kind == 'watch' else 'investigación'})", ""]
        if self.spec:
            lines.append(self.spec.describe())
        elif self.research:
            r = self.research
            lines += [f"Investigación: {r['objective']}", f"  Prestar atención a: {r['focus'] or '—'}",
                      f"  Dominios excluidos: {', '.join(r['exclude_domains']) or '—'}",
                      f"  Profundidad: {r['depth']} (máx. {r['budget']['max_rounds']} rondas) · "
                      f"coste máximo ${r['budget']['max_cost_usd']:.2f}"]
        for title, items in (("Supuestos", self.assumptions), ("Interpretaciones posibles", self.interpretations),
                             ("Sin resolver", self.unresolved), ("Avisos", self.warnings),
                             ("Requiere tu decisión", self.requires_user_action)):
            if items:
                lines += ["", f"{title}:"] + [f"  - {x}" for x in items]
        return "\n".join(lines)


def slugify(text: str, fallback: str = "seguimiento") -> str:
    norm = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", norm).strip("-")[:40].strip("-")
    return slug or fallback


def draft_from_text(app: App, text: str, *, kind: str | None = None, name: str | None = None,
                    current: WatchSpec | None = None, extra_sources: list[SourceRef] | None = None,
                    now: datetime | None = None, ledger: Ledger | None = None) -> ConfigurationDraft:
    text = " ".join(text.split())
    if not text:
        raise ValueError("describe qué quieres investigar o seguir")
    tz = current.timezone if current else app.settings.timezone
    user = f"User instruction:\n{text}\n\nUser timezone: {tz}\n"
    if kind:
        user += f"\nThe user asked for kind = {kind}.\n"
    if current:
        user += ("\nCurrent configuration of the watch being edited (keep everything the user does not change):\n"
                 + current.model_dump_json(indent=1) + "\n")
    data = app.llm("orchestrator").complete_json(purpose="configure", system=CONFIGURE_SYSTEM, user=user,
                                                 schema=CONFIGURE_SCHEMA, max_tokens=6_000, ledger=ledger)
    now = now or datetime.now().astimezone()
    draft = ConfigurationDraft(kind=kind or data["kind"], input_text=text, assumptions=list(data["assumptions"]),
                               unresolved=list(data["unresolved"]), interpretations=list(data["interpretations"]))
    if draft.kind == "research":
        _research(app, draft, data)
    else:
        _watch(app, draft, data, name=name or (current.name if current else None), current=current,
               extra_sources=extra_sources or [], now=now, tz=tz)
    _review(app, draft)
    draft.draft_id = app.db.insert("config_drafts", {"kind": draft.kind, "input_text": text,
                                                     "draft_json": dumps(draft.to_dict()), "status": "pending",
                                                     "target": draft.spec.name if draft.spec else None,
                                                     "created_at": now_iso(), "updated_at": now_iso()})
    return draft


def mark_draft(app: App, draft: ConfigurationDraft, status: str) -> None:
    if draft.draft_id:
        app.db.execute("UPDATE config_drafts SET status=?, updated_at=? WHERE id=?", (status, now_iso(), draft.draft_id))


def _research(app: App, draft: ConfigurationDraft, data: dict) -> None:
    depth = data["depth"] if data["depth"] in DEPTH_ROUNDS else "standard"
    budget = app.settings.budget.to_dict()
    budget["max_rounds"] = DEPTH_ROUNDS[depth]
    if data["max_cost_usd"] and data["max_cost_usd"] > 0:
        budget["max_cost_usd"] = float(data["max_cost_usd"])
    draft.research = {"objective": data["objective"].strip() or draft.input_text, "focus": data["focus"].strip(),
                      "exclude_domains": [d.strip().lower() for d in data["exclude_domains"] if d.strip()],
                      "depth": depth, "budget": budget}


def _watch(app: App, draft: ConfigurationDraft, data: dict, *, name: str | None, current: WatchSpec | None,
           extra_sources: list[SourceRef], now: datetime, tz: str) -> None:
    sources = []
    for s in data["sources"]:
        try:
            sources.append(SourceRef(kind=s["kind"], locator=s["locator"], label=s.get("label", "")))
        except ValidationError as exc:
            draft.warnings.append(f"fuente descartada ({s.get('kind')}:{s.get('locator')}): {_first_error(exc)}")
    sources += extra_sources
    if not sources:
        draft.unresolved.append("No se indicó ninguna fuente concreta: añade fuentes (URL, r/subreddit, canal de "
                                "YouTube, feed o búsqueda) con --source.")
        draft.unresolved = list(dict.fromkeys(draft.unresolved))
        return
    base = current.model_dump() if current else {}
    digest = DigestPolicy(mode=data["digest_mode"], at=data["digest_at"] or "20:00",
                          weekday=min(6, max(0, data["digest_weekday"])),
                          every_hours=data["digest_every_hours"] if data["digest_every_hours"] > 0 else 24,
                          min_events=max(1, data["digest_min_events"]))
    alerts = AlertRules(enabled=data["alert_enabled"], min_importance=min(1.0, max(0.0, data["alert_min_importance"])),
                        min_independent_sources=max(0, data["alert_min_independent_sources"]),
                        keywords=[k for k in data["alert_keywords"] if k.strip()])
    expires = None
    if data["duration_days"] and data["duration_days"] > 0:
        expires = iso(now + timedelta(days=data["duration_days"]))
    values = {**base, "name": name or slugify(data["name"] or data["objective"]), "objective": data["objective"],
              "sources": [s.model_dump() for s in sources], "topics": data["topics"], "exclusions": data["exclusions"],
              "every_minutes": max(5, data["every_minutes"] or 240), "timezone": tz, "depth": data["depth"],
              "alerts": alerts.model_dump(), "digest": digest.model_dump(), "expires_at": expires}
    if data["max_cost_usd"] and data["max_cost_usd"] > 0:
        values["max_cost_usd_per_run"] = float(data["max_cost_usd"])
    try:
        draft.spec = WatchSpec.model_validate(values)
    except ValidationError as exc:
        draft.unresolved.append(f"configuración no válida: {_first_error(exc)}")


def _first_error(exc: ValidationError) -> str:
    err = exc.errors()[0]
    return f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"


def _review(app: App, draft: ConfigurationDraft) -> None:
    """Deterministic checks that the model cannot override."""
    if len(draft.interpretations) > 1:
        draft.requires_user_action.append("Hay varias interpretaciones con consecuencias distintas: elige una.")
    if draft.unresolved:
        draft.requires_user_action.append("Faltan datos para completar la configuración (ver 'Sin resolver').")
    spec = draft.spec
    if spec is None:
        return
    for ref in spec.sources:
        try:
            connector = app.connector(ref.kind)
        except RosError as exc:
            draft.requires_user_action.append(f"Fuente {ref.key()}: {exc.message}")
            continue
        status = connector.status() if hasattr(connector, "status") else None
        if status and status.state not in ("ready", "degraded"):
            draft.requires_user_action.append(f"Fuente {ref.key()}: conector {status.state} — {status.detail}")
    runs_per_day = 1440 / spec.every_minutes
    worst = runs_per_day * spec.max_cost_usd_per_run
    cap = app.settings.monitor_daily_cost_usd
    if worst > cap:
        draft.warnings.append(f"En el peor caso este seguimiento gastaría ${worst:.2f}/día; el tope global es "
                              f"${cap:.2f}/día: el análisis se aplazará al alcanzarlo.")
    draft.requires_user_action.append("Confirmar el trabajo recurrente antes de activarlo.")


def diff_specs(old: WatchSpec | None, new: WatchSpec) -> str:
    import difflib
    a = json.dumps(old.model_dump(), indent=2, ensure_ascii=False).splitlines() if old else []
    b = json.dumps(new.model_dump(), indent=2, ensure_ascii=False).splitlines()
    return "\n".join(difflib.unified_diff(a, b, "actual", "propuesto", lineterm="", n=1))
