"""Deterministic stand-in for the model (`llm = "fake"`).

It runs the whole pipeline without an API key or cost, to try ROS out and to test it.
It does no real analysis: claims are the document's first sentences and findings simply
group them. Every output is valid against the schemas in `ros.research.prompts`.
"""

from __future__ import annotations

import re
import unicodedata

_CLAIM_LINE = re.compile(r"^C(\d+) \[(\w+)\] \(([^,)]*)", re.M)
_SUBQ = re.compile(r"^\[([\w.-]+)\] (.+)$", re.M)


def _objective(user: str) -> str:
    m = re.search(r"objective:\s*\n(.+)", user, re.I)
    return (m.group(1) if m else user.splitlines()[0]).strip()


def _document(user: str) -> str:
    m = re.search(r"<external_content[^>]*>\n(.*)\n</external_content>", user, re.S)
    return m.group(1) if m else ""


def _sentences(text: str) -> list[str]:
    body = "\n".join(ln for ln in text.splitlines() if not ln.startswith(("TITLE:", "URL:")))
    parts = re.split(r"(?<=[.!?])\s+", " ".join(body.split()))
    return [p for p in parts if 40 <= len(p) <= 300]


def plan(user: str) -> dict:
    obj = _objective(user)
    return {
        "interpretation": f"Investigar: {obj}",
        "assumptions": ["modo offline: el plan es genérico"],
        "subquestions": [
            {"id": "q1", "question": f"¿Qué es y en qué estado está {obj}?", "priority": "high"},
            {"id": "q2", "question": f"¿Qué riesgos y críticas existen sobre {obj}?", "priority": "medium"},
        ],
        "key_terms": obj.split()[:5],
        "queries": [
            {"text": obj, "rationale": "consulta directa", "subquestion_id": "q1"},
            {"text": f"{obj} críticas problemas", "rationale": "visión crítica", "subquestion_id": "q2"},
        ],
        "stop_criteria": "dos rondas o sin información nueva",
        "focus": _focus(obj),
        "excluded_domains": [],
        "alternative_interpretations": [],
    }


def extract(user: str) -> dict:
    sentences = _sentences(_document(user))[:3]
    return {
        "relevant": bool(sentences),
        "summary": sentences[0] if sentences else "",
        "claims": [{"text": s, "type": "fact", "quote": s[:200], "subquestion_id": "q1", "confidence": 0.5}
                   for s in sentences],
        "entities": [],
        "new_terms": [],
        "source_kind": "unknown",
        "authority": "medium",
        "conflicts_of_interest": "",
        "injection_suspected": "ignore previous instructions" in user.lower(),
    }


def analyze(user: str) -> dict:
    claims = _CLAIM_LINE.findall(user)
    ids = [int(c[0]) for c in claims]
    subqs = _SUBQ.findall(user)
    round_n = int(m.group(1)) if (m := re.search(r"Round just completed: (\d+)", user)) else 1
    findings = [{"title": f"Hallazgo {k + 1}", "summary": f"Agrupa las afirmaciones {ids[k:k + 3]}",
                 "confidence": "medium", "claim_ids": ids[k:k + 3]} for k in range(0, len(ids), 3)]
    return {
        "findings": findings,
        "contradictions": [],
        "gaps": [] if ids else ["no se encontró evidencia"],
        "subquestion_status": [{"id": i, "status": "partial" if ids else "open", "note": ""} for i, _ in subqs],
        "new_subtopics": [],
        "next_queries": [] if round_n >= 2 else [
            {"text": f"{_objective(user)} datos oficiales", "rationale": "fuentes primarias", "subquestion_id": "q1"}],
        "drop_lines": [],
        "coverage": 0.7 if round_n >= 2 else 0.4,
        "should_stop": round_n >= 2,
        "stop_reason": "modo offline: dos rondas",
    }


def synthesize(user: str) -> dict:
    ids = [int(c[0]) for c in _CLAIM_LINE.findall(user)]
    cites = " ".join(f"[C{i}]" for i in ids[:5])
    return {
        "title": f"Informe (offline): {_objective(user)}",
        "executive_summary": f"Informe generado sin modelo de lenguaje a partir de {len(ids)} afirmaciones. {cites}",
        "sections": [{"heading": "Evidencia recopilada", "body": f"Ver las afirmaciones citadas: {cites}"}],
        "conclusions": [],
        "uncertainties": ["El modo offline no evalúa la evidencia."],
        "open_questions": [],
        "next_steps": ["Repetir con un modelo real (ROS_LLM=anthropic)."],
    }


def validate(user: str) -> dict:
    return {"conclusions": [], "issues": [], "overall": "ok", "note": "modo offline: validación no realizada"}


# ---------------------------------------------------------------- monitoring

def _words(text: str) -> set[str]:
    return {_fold(w) for w in re.findall(r"\w+", text.lower()) if len(w) >= 4}


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def triage(user: str) -> dict:
    interest = re.search(r"Watch interest:\n(.+?)\n\nTopics to detect: (.*?)\n", user, re.S)
    wanted = _words((interest.group(1) + " " + interest.group(2)) if interest else "")
    doc = _document(user)
    title = (re.search(r"^TITLE: (.*)$", doc, re.M) or re.search(r"(.*)", doc)).group(1).strip()
    body = "\n".join(ln for ln in doc.splitlines() if not re.match(r"^[A-Z]+: ", ln))
    hits = sorted(wanted & _words(title + " " + body))
    sentences = _sentences(body) or [" ".join(body.split())[:300]]
    rumor = bool(re.search(r"\b(rumor|rumou?r|se rumorea|podría|filtraci[oó]n|leak)", body, re.I))
    return {
        "relevant": bool(hits),
        "relevance": min(1.0, 0.45 + 0.15 * len(hits)) if hits else 0.1,
        "importance": min(0.8, 0.4 + 0.1 * len(hits)) if hits else 0.1,
        "title": title[:200] or "(sin título)",
        "summary": sentences[0][:300] if sentences else "",
        "claim_type": "rumor" if rumor else "fact",
        "labels": hits[:5],
        "entities": sorted({w for w in re.findall(r"\b[A-Z][\w-]{2,}\b", title)})[:8],
        "claims": [{"text": s, "type": "rumor" if rumor else "fact", "quote": s[:200], "confidence": 0.5}
                   for s in sentences[:2]],
        "why": f"coincide con el interés del seguimiento: {', '.join(hits[:5])}" if hits else "",
        "injection_suspected": "ignore previous instructions" in user.lower(),
    }


def cluster(user: str) -> dict:
    events = re.findall(r"^E(\d+) \[[^\]]*\] (.+?): (.*)$", user, re.M)
    ids = [int(e[0]) for e in events]
    return {"member_event_ids": ids, "title": events[0][1] if events else "Grupo",
            "summary": " / ".join(e[1] for e in events)[:500], "relation": "tratan el mismo asunto (modo offline)",
            "confirmed": [], "uncertain": [], "contradictions": [], "why_relevant": "",
            "watch_next": [], "importance": 0.6}


def digest(user: str) -> dict:
    groups = re.findall(r"^G(\d+) — (.+?) \(importance", user, re.M)
    return {"title": "Informe de seguimiento (offline)",
            "executive_summary": f"{len(groups)} asuntos detectados en el periodo (resumen sin modelo de lenguaje).",
            "developments": [{"group_id": int(g), "headline": t, "body": ""} for g, t in groups[:10]],
            "connections": [], "contradictions": [], "implications": [], "changes_since_previous": [],
            "open_questions": [], "watch_next": [], "confidence": "low",
            "confidence_note": "modo offline: sin evaluación de la evidencia"}


# ---------------------------------------------------------------- natural-language configuration

NUMBERS = {"un": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "ocho": 8, "diez": 10,
           "doce": 12, "quince": 15, "veinte": 20, "treinta": 30, "veinticuatro": 24}
UNITS = {"minuto": 1, "hora": 60, "dia": 1440, "semana": 10080}


def _number(token: str) -> int | None:
    token = _fold(token)
    return int(token) if token.isdigit() else NUMBERS.get(token)


def _focus(text: str) -> str:
    m = re.search(r"(?:presta(?:ndo)? (?:especial )?atenci[oó]n a|c[eé]ntrate en|especialmente en)\s+(.+?)(?:\.|$)",
                  text, re.I)
    return m.group(1).strip() if m else ""


def _split_list(text: str) -> list[str]:
    parts = re.split(r",\s*|\s+y\s+|\s+o\s+", text.strip().rstrip("."))
    return [p.strip() for p in parts if p.strip()]


def configure(user: str) -> dict:
    m = re.search(r"User instruction:\n(.+?)\n\n", user, re.S)
    text = (m.group(1) if m else user).strip()
    low = _fold(text)
    asked = re.search(r"kind = (\w+)", user)
    research = bool(re.match(r"\s*(investiga|analiza|estudia|research)", low)) and not re.search(
        r"\b(sigue|vigila|revisa|avisame|monitoriza)\b", low)
    kind = asked.group(1) if asked else ("research" if research else "watch")
    assumptions, unresolved = [], []

    sources = []
    for url in re.findall(r"https?://[^\s,;]+", text):
        from .monitor.spec import guess_kind
        url = url.rstrip(".)")
        sources.append({"kind": guess_kind(url), "locator": url, "label": ""})
    for sub in re.findall(r"(?<![\w/])r/([A-Za-z0-9_]{2,21})", text):
        sources.append({"kind": "reddit", "locator": f"r/{sub}", "label": ""})
    for handle in re.findall(r"(?<![\w@])@([A-Za-z0-9_.-]{2,40})", text):
        platform = "youtube" if "youtube" in low else "instagram" if "instagram" in low else \
            "x" if re.search(r"\b(x|twitter)\b", low) else None
        if platform:
            sources.append({"kind": platform, "locator": f"@{handle}", "label": ""})
        else:
            unresolved.append(f"¿En qué plataforma está la cuenta @{handle}?")
    if not sources and re.search(r"\b(estas|estos|esas|esos)\s+(cuentas|fuentes|canales|paginas|webs|sitios|feeds)",
                                 low):
        unresolved.append("La instrucción menciona fuentes que no se incluyeron ('estas cuentas/fuentes').")

    every = 240
    em = re.search(r"cada\s+(\w+)\s+(minuto|hora|dia|semana)s?", low)
    if em and _number(em.group(1)):
        every = _number(em.group(1)) * UNITS[em.group(2)]
    elif re.search(r"cada\s+(hora|minuto|dia|semana)\b", low):
        every = UNITS[re.search(r"cada\s+(hora|minuto|dia|semana)\b", low).group(1)]
    elif "cada media hora" in low:
        every = 30
    else:
        assumptions.append("Frecuencia de revisión no indicada: cada 4 horas.")

    mode, at, weekday, min_events = "daily", "20:00", 0, 1
    hm = re.search(r"a las (\d{1,2})(?::(\d{2}))?", low)
    if "final del dia" in low or "fin del dia" in low:
        at = "21:00"
        assumptions.append("'Al final del día' se interpreta como las 21:00.")
    elif hm:
        at = f"{int(hm.group(1)):02d}:{hm.group(2) or '00'}"
    elif re.search(r"\b(semanal|cada semana|semanalmente)\b", low):
        mode, at = "weekly", "09:00"
    elif am := re.search(r"(?:cuando se acumulen|al acumular)\s+(\w+)", low):
        mode, min_events = "threshold", _number(am.group(1)) or 5
    else:
        assumptions.append("Horario del informe no indicado: un informe diario a las 20:00.")

    immediate = bool(re.search(r"inmediat|al momento|en cuanto|urgente", low))
    keywords = []
    km = re.search(r"si (?:aparece|hay|se publica|sale)\s+(?:una?\s+)?(.+?)(?:\s+o\s+si\b|,|\.|$)", text, re.I)
    if km and immediate:
        keywords.append(km.group(1).strip())
    independent = 2 if re.search(r"(varias|multiples|distintas|diferentes)\s+fuentes", low) else 0

    topics = []
    tm = re.search(r"(?:detecta|me interesa(?:n)?(?: cualquier anuncio sobre)?|interesa(?:n)?)\s+(.+?)(?:\.|$)", text, re.I)
    if tm:
        topics = _split_list(tm.group(1))
    focus = _focus(text)
    if kind == "research" and focus:
        topics = _split_list(focus)
    exclusions = []
    xm = re.search(r"(?:ignora|excluye|sin)\s+(.+?)(?:\.|$)", text, re.I)
    if xm and kind == "watch":
        exclusions = _split_list(xm.group(1))
    duration = 0
    dm = re.search(r"durante\s+(\w+)\s+(dia|semana|mes)", low)
    if dm and _number(dm.group(1)):
        duration = _number(dm.group(1)) * {"dia": 1, "semana": 7, "mes": 30}[dm.group(2)]
    objective = text
    return {
        "kind": kind, "name": " ".join(re.findall(r"\w+", topics[0] if topics else text)[:4]), "objective": objective,
        "focus": focus, "exclude_domains": [], "sources": sources, "topics": topics, "exclusions": exclusions,
        "every_minutes": every, "digest_mode": mode, "digest_at": at, "digest_weekday": weekday,
        "digest_every_hours": 24, "digest_min_events": min_events, "alert_enabled": True,
        "alert_min_importance": 0.8 if immediate else 0.85, "alert_min_independent_sources": independent,
        "alert_keywords": keywords, "depth": "standard", "max_cost_usd": 0, "duration_days": duration,
        "assumptions": assumptions, "unresolved": unresolved, "interpretations": [],
    }


HANDLERS = {"plan": plan, "extract": extract, "analyze": analyze, "synthesize": synthesize, "validate": validate,
            "triage": triage, "cluster": cluster, "digest": digest, "configure": configure}


def offline_handler(purpose: str, system: str, user: str, schema: dict) -> dict:
    try:
        return HANDLERS[purpose.split(":")[0]](user)
    except KeyError:
        raise ValueError(f"offline mode has no handler for {purpose!r}") from None
