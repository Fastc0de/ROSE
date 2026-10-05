"""Deterministic stand-in for the model (`llm = "fake"`).

It runs the whole pipeline without an API key or cost, to try ROS out and to test it.
It does no real analysis: claims are the document's first sentences and findings simply
group them. Every output is valid against the schemas in `ros.research.prompts`.
"""

from __future__ import annotations

import re

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


HANDLERS = {"plan": plan, "extract": extract, "analyze": analyze, "synthesize": synthesize, "validate": validate}


def offline_handler(purpose: str, system: str, user: str, schema: dict) -> dict:
    try:
        return HANDLERS[purpose.split(":")[0]](user)
    except KeyError:
        raise ValueError(f"offline mode has no handler for {purpose!r}") from None
