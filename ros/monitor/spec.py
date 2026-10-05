"""WatchSpec: the reviewable, versioned configuration of one watch (seguimiento).

Everything a watch does is decided here and validated by code: sources, interest, cadence,
alert rules, digest schedule, budget and duration. The model may *propose* a spec from natural
language (see `ros.monitor.drafts`), but only a validated WatchSpec is ever stored or executed.
"""

from __future__ import annotations

import re
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
WEEKDAYS = {"lun": 0, "mon": 0, "mar": 1, "tue": 1, "mie": 2, "mié": 2, "wed": 2, "jue": 3, "thu": 3,
            "vie": 4, "fri": 4, "sab": 5, "sáb": 5, "sat": 5, "dom": 6, "sun": 6}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceRef(_Model):
    kind: str
    locator: str
    label: str = ""

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        v = v.strip().lower()
        if not re.fullmatch(r"[a-z_]{2,20}", v):
            raise ValueError(f"tipo de fuente no válido: {v!r}")
        return v

    @field_validator("locator")
    @classmethod
    def _locator(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) > 500:
            raise ValueError("localizador de fuente vacío o demasiado largo")
        return v

    def key(self) -> str:
        return f"{self.kind}:{self.locator}"


class AlertRules(_Model):
    enabled: bool = True
    min_importance: float = Field(0.85, ge=0, le=1)   # alert when one event is at least this important
    min_independent_sources: int = Field(0, ge=0)     # alert when N independent hosts report one cluster (0 = off)
    keywords: list[str] = []                           # alert when an event mentions any of these
    max_per_day: int = Field(5, ge=0)                  # attention budget: extra alerts go to the digest


class DigestPolicy(_Model):
    mode: Literal["daily", "weekly", "interval", "threshold", "manual"] = "daily"
    at: str = "20:00"                       # local time (watch timezone) for daily/weekly
    weekday: int = Field(0, ge=0, le=6)     # 0 = Monday (weekly)
    every_hours: float = Field(24, gt=0)    # interval mode
    min_events: int = Field(1, ge=1)        # threshold mode, and the minimum to send any digest
    max_events: int = Field(60, ge=1)       # events detailed in one digest (the rest are counted)

    @field_validator("at")
    @classmethod
    def _at(cls, v: str) -> str:
        m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?", v.strip())
        if not m or int(m.group(1)) > 23 or int(m.group(2) or 0) > 59:
            raise ValueError(f"hora no válida: {v!r} (usa HH:MM)")
        return f"{int(m.group(1)):02d}:{int(m.group(2) or 0):02d}"


class WatchSpec(_Model):
    name: str
    objective: str                          # what matters, in the user's words
    sources: list[SourceRef] = Field(min_length=1)
    topics: list[str] = []                  # what to detect (launches, price changes...)
    exclusions: list[str] = []              # what to ignore
    every_minutes: int = Field(240, ge=5)
    timezone: str = "UTC"
    depth: Literal["light", "standard", "deep"] = "standard"
    min_relevance: float = Field(0.5, ge=0, le=1)
    group_window_hours: float = Field(72, gt=0)
    alerts: AlertRules = AlertRules()
    digest: DigestPolicy = DigestPolicy()
    backfill: int = Field(5, ge=0)          # on a source's first sync, analyse only the N newest items
    max_items_per_run: int = Field(40, ge=1)
    max_cost_usd_per_run: float = Field(0.50, gt=0)
    retention_days: int = Field(365, ge=1)
    expires_at: str | None = None           # ISO date/time; None = indefinite
    output_format: Literal["markdown"] = "markdown"

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip().lower()
        if not NAME_RE.match(v):
            raise ValueError(f"nombre no válido: {v!r} (minúsculas, números, '-' y '_')")
        return v

    @field_validator("objective")
    @classmethod
    def _objective(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("el seguimiento necesita describir qué te interesa")
        return v[:2000]

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"zona horaria desconocida: {v!r}") from exc
        return v

    @model_validator(mode="after")
    def _unique_sources(self) -> "WatchSpec":
        seen, unique = set(), []
        for s in self.sources:
            if s.key() not in seen:
                seen.add(s.key())
                unique.append(s)
        self.sources = unique
        return self

    def describe(self) -> str:
        """Human-readable summary used for review before saving or enabling."""
        d = self.digest
        when = {"daily": f"diario a las {d.at}", "weekly": f"semanal ({_WEEKDAY_NAMES[d.weekday]} {d.at})",
                "interval": f"cada {d.every_hours:g} h", "threshold": f"al acumular {d.min_events} eventos",
                "manual": "solo bajo demanda"}[d.mode]
        a = self.alerts
        alert_bits = []
        if a.enabled:
            alert_bits.append(f"importancia ≥ {a.min_importance:g}")
            if a.min_independent_sources:
                alert_bits.append(f"≥ {a.min_independent_sources} fuentes independientes sobre lo mismo")
            if a.keywords:
                alert_bits.append("palabras clave: " + ", ".join(a.keywords))
        lines = [
            f"Seguimiento: {self.name}",
            f"  Interés: {self.objective}",
            "  Fuentes:",
            *[f"    - {s.kind}: {s.locator}" + (f" ({s.label})" if s.label else "") for s in self.sources],
            f"  Temas: {', '.join(self.topics) or '—'}",
            f"  Excluir: {', '.join(self.exclusions) or '—'}",
            f"  Frecuencia: cada {format_minutes(self.every_minutes)} · profundidad {self.depth}",
            f"  Informe: {when} ({self.timezone})",
            f"  Alertas inmediatas: {'; '.join(alert_bits) if alert_bits else 'desactivadas'}",
            f"  Presupuesto: ${self.max_cost_usd_per_run:.2f} por ejecución, {self.max_items_per_run} items máx.",
            f"  Duración: {('hasta ' + self.expires_at) if self.expires_at else 'indefinida'}"
            f" · retención {self.retention_days} días",
        ]
        return "\n".join(lines)


_WEEKDAY_NAMES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def format_minutes(minutes: int) -> str:
    if minutes % 1440 == 0:
        return f"{minutes // 1440} d"
    if minutes % 60 == 0:
        return f"{minutes // 60} h"
    return f"{minutes} min"


def parse_interval(text: str) -> int:
    """'30m', '4h', '1d', '90' (minutes) -> minutes."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(m|min|h|d)?\s*", text.lower())
    if not m:
        raise ValueError(f"intervalo no válido: {text!r} (ejemplos: 30m, 4h, 1d)")
    value, unit = float(m.group(1)), m.group(2) or "m"
    return int(value * {"m": 1, "min": 1, "h": 60, "d": 1440}[unit])


def parse_digest(text: str) -> DigestPolicy:
    """'daily@20:00', 'weekly@mon@09:00', 'every:12h', 'threshold:5', 'manual'."""
    t = text.strip().lower()
    if t == "manual":
        return DigestPolicy(mode="manual")
    if t.startswith("daily"):
        return DigestPolicy(mode="daily", at=t.split("@", 1)[1] if "@" in t else "20:00")
    if t.startswith("weekly"):
        parts = t.split("@")
        day = WEEKDAYS.get(parts[1][:3], None) if len(parts) > 1 else 0
        if day is None:
            raise ValueError(f"día de la semana no válido en {text!r}")
        return DigestPolicy(mode="weekly", weekday=day, at=parts[2] if len(parts) > 2 else "09:00")
    if t.startswith("every:"):
        return DigestPolicy(mode="interval", every_hours=parse_interval(t[6:]) / 60)
    if t.startswith("threshold:"):
        return DigestPolicy(mode="threshold", min_events=int(t[10:]))
    raise ValueError(f"horario de informe no válido: {text!r} (daily@20:00, weekly@mon@09:00, every:12h, "
                     "threshold:5, manual)")


def parse_source(text: str) -> SourceRef:
    """'rss:https://x/feed', 'reddit:r/python', 'youtube:@canal' or a bare URL (kind guessed)."""
    text = text.strip()
    m = re.match(r"^([a-z_]{2,20}):(?!//)(.+)$", text)
    if m:
        return SourceRef(kind=m.group(1), locator=m.group(2))
    return SourceRef(kind=guess_kind(text), locator=text)


def guess_kind(locator: str) -> str:
    low = locator.lower()
    if re.search(r"(^|/)r/[a-z0-9_]+", low) and ("reddit" in low or low.startswith("r/")):
        return "reddit"
    if "youtube.com" in low or "youtu.be" in low:
        return "youtube"
    if re.search(r"(\.rss|\.xml|/feed/?|/rss/?|atom)(\?|$)", low):
        return "rss"
    if low.startswith(("http://", "https://")):
        return "web"
    return "search"
