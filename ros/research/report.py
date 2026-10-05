"""Markdown rendering for research runs. Every statement is traceable to a claim and source."""

from __future__ import annotations

import re

from ..connectors.feeds import host_of
from ..db import Database, loads

TYPE_LABELS = [
    ("fact", "Hechos"),
    ("inference", "Inferencias"),
    ("opinion", "Opiniones"),
    ("prediction", "Predicciones"),
    ("rumor", "Rumores (no confirmados)"),
]
STATUS_ICON = {"verified": "✅ verificado", "unverified": "· sin verificar", "disputed": "⚠ disputado",
               "superseded": "↺ reemplazado"}
CONF = {"high": "alta", "medium": "media", "low": "baja"}


def _cite(text: str) -> str:
    return re.sub(r"\[C(\d+)\]", r"[C\1](#c\1)", text)


SUPPORT = {"partial": "⚠ respaldo parcial según el validador", "no": "✖ no respaldada según el validador"}
OVERALL = {"ok": "✅ sin problemas", "minor_issues": "⚠ problemas menores", "major_issues": "✖ problemas importantes"}


def render_report(db: Database, run_id: int, *, synthesis: dict | None, stop_reason: str, degraded: list[str],
                  status: str, usage: dict, validation: dict | None = None) -> str:
    run = db.one("SELECT * FROM runs WHERE id=?", (run_id,))
    plan = loads(run["plan_json"], {}) or {}
    out: list[str] = []
    title = (synthesis or {}).get("title") or run["objective"]
    out.append(f"# {title}\n")
    out.append(f"> **Objetivo:** {run['objective']}  ")
    state = {"completed": "✅ Completa", "partial": "◐ Parcial", "cancelled": "■ Cancelada"}.get(status, "✖ Fallida")
    out.append(f"> **Estado:** {state} · investigación #{run_id} · {run['created_at'][:16].replace('T', ' ')} UTC  ")
    if stop_reason:
        out.append(f"> **Motivo de cierre:** {stop_reason}  ")
    if degraded:
        out.append(f"> **Limitaciones:** {'; '.join(degraded)}. Este informe NO debe considerarse completo.")
    out.append("")

    last_round = db.one("SELECT MAX(round_n) m FROM findings WHERE run_id=?", (run_id,))["m"]
    findings = db.all("SELECT * FROM findings WHERE run_id=? AND round_n=? ORDER BY id", (run_id, last_round or 0))

    if synthesis:
        out.append("## Resumen ejecutivo\n")
        out.append(_cite(synthesis["executive_summary"]) + "\n")
        for sec in synthesis["sections"]:
            out.append(f"## {sec['heading']}\n")
            out.append(_cite(sec["body"]) + "\n")
        if synthesis["conclusions"]:
            out.append("## Conclusiones\n")
            checks = {v["index"]: v for v in (validation or {}).get("conclusions", [])}
            for k, c in enumerate(synthesis["conclusions"]):
                refs = " ".join(f"[C{i}](#c{i})" for i in c["claim_ids"])
                out.append(f"- **({CONF.get(c['confidence'], c['confidence'])})** {_cite(c['text'])} {refs}")
                check = checks.get(k)
                if check and check["supported"] in SUPPORT:
                    out.append(f"  - _{SUPPORT[check['supported']]}: {check['issue']}_")
            out.append("")
    else:
        out.append("## Hallazgos\n")
        out.append("_Síntesis del modelo no disponible; se muestran los hallazgos consolidados de la última ronda._\n")

    main = [f for f in findings if f["kind"] == "finding"]
    if main:
        out.append("## Hallazgos por ronda de análisis\n" if synthesis else "")
        for f in main:
            refs = " ".join(f"[C{i}](#c{i})" for i in loads(f["claim_ids_json"]))
            out.append(f"- **{f['title']}** ({CONF.get(f['confidence'], f['confidence'])}): {f['summary']} {refs}")
        out.append("")
    contra = [f for f in findings if f["kind"] == "contradiction"]
    if contra:
        out.append("## Contradicciones\n")
        for f in contra:
            refs = " ".join(f"[C{i}](#c{i})" for i in loads(f["claim_ids_json"]))
            out.append(f"- {f['summary']} {refs}")
        out.append("")
    discoveries = db.all("SELECT * FROM findings WHERE run_id=? AND kind='discovery' ORDER BY id", (run_id,))
    if discoveries:
        out.append("## Descubrimientos (temas que no estaban en la pregunta original)\n")
        for d in discoveries:
            out.append(f"- **{d['title']}** — {d['summary']} _(ronda {d['round_n']})_")
        out.append("")
    if synthesis:
        for key, heading in (("uncertainties", "Incertidumbres"), ("open_questions", "Preguntas abiertas"),
                             ("next_steps", "Qué seguir investigando o vigilando")):
            if synthesis[key]:
                out.append(f"## {heading}\n")
                out.extend(f"- {_cite(x)}" for x in synthesis[key])
                out.append("")
    if synthesis:
        out.append("## Validación\n")
        if validation:
            out.append(f"**Resultado:** {OVERALL.get(validation['overall'], validation['overall'])}. {validation['note']}\n")
            out.extend(f"- _{i['location']}_: {i['problem']}" for i in validation["issues"])
        else:
            out.append("_El informe no pudo validarse contra la evidencia (ver errores)._")
        out.append("")
    gaps = [f for f in findings if f["kind"] == "gap"]
    if gaps and not synthesis:
        out.append("## Vacíos de información\n")
        out.extend(f"- {g['summary']}" for g in gaps)
        out.append("")

    # Evidence -------------------------------------------------------------
    out.append("## Evidencia\n")
    out.append("Cada afirmación indica su tipo, su estado de verificación y la fuente exacta.\n")
    claims = db.all("""SELECT c.*, i.url, i.title item_title FROM claims c JOIN items i ON i.id=c.item_id
                       WHERE c.run_id=? ORDER BY c.id""", (run_id,))
    for ctype, label in TYPE_LABELS:
        group = [c for c in claims if c["claim_type"] == ctype]
        if not group:
            continue
        out.append(f"### {label} ({len(group)})\n")
        for c in group:
            out.append(f'- <a id="c{c["id"]}"></a>**C{c["id"]}** {c["text"]} — {STATUS_ICON[c["status"]]} · '
                       f'[{host_of(c["url"])}]({c["url"]})')
            if c["quote"]:
                out.append(f'  > “{c["quote"]}”')
        out.append("")

    # Sources --------------------------------------------------------------
    rows = db.all("""SELECT ri.*, i.meta_json FROM run_items ri LEFT JOIN items i ON i.id=ri.item_id
                     WHERE ri.run_id=? AND ri.status NOT IN ('candidate','rejected') ORDER BY ri.round_n, ri.id""", (run_id,))
    out.append("## Fuentes consultadas\n")
    out.append("| # | Ronda | Fuente | Tipo | Autoridad | Estado |")
    out.append("|---|---|---|---|---|---|")
    for k, r in enumerate(rows, 1):
        meta = loads(r["meta_json"], {}) or {}
        title = (r["title"] or r["url"]).replace("|", "/")[:80]
        st = {"extracted": "analizada", "irrelevant": "no relevante", "failed": "fallida", "reused": "reutilizada",
              "fetched": "descargada", "selected": "pendiente"}.get(r["status"], r["status"])
        if r["reason"] and r["status"] in ("failed", "irrelevant"):
            st += f" ({r['reason'][:60]})"
        out.append(f"| {k} | {r['round_n']} | [{title}]({r['url']}) | {meta.get('source_kind', '—')} | "
                   f"{meta.get('authority', '—')} | {st} |")
    pending = db.one("SELECT COUNT(*) c FROM run_items WHERE run_id=? AND status='candidate'", (run_id,))["c"]
    rejected = db.one("SELECT COUNT(*) c FROM run_items WHERE run_id=? AND status='rejected'", (run_id,))["c"]
    out.append(f"\nCandidatas no descargadas: {pending} · rechazadas por política: {rejected}\n")

    # Process --------------------------------------------------------------
    out.append("## Proceso de investigación\n")
    if plan.get("interpretation"):
        out.append(f"**Interpretación:** {plan['interpretation']}\n")
    if plan.get("assumptions"):
        out.append("**Supuestos:** " + "; ".join(plan["assumptions"]) + "\n")
    out.append("**Sub-preguntas:**\n")
    for sq in plan.get("subquestions", []):
        origin = " _(descubierta)_" if sq.get("origin") == "discovery" else ""
        out.append(f"- [{sq['id']}] {sq['question']} — {sq.get('status', 'open')}{origin}")
    out.append("")
    for rnd in db.all("SELECT * FROM rounds WHERE run_id=? ORDER BY n", (run_id,)):
        qs = db.all("SELECT text, status, results FROM queries WHERE run_id=? AND round_n=?", (run_id, rnd["n"]))
        an = loads(rnd["analysis_json"], {}) or {}
        cov = f", cobertura {an['coverage']:.0%}" if "coverage" in an else ""
        out.append(f"**Ronda {rnd['n']}** ({rnd['stage']}{cov}): " +
                   "; ".join(f"“{q['text']}” ({q['results'] if q['results'] is not None else '–'})" for q in qs))
    versions = db.all("SELECT version, round_n, reason FROM plan_versions WHERE run_id=? ORDER BY version", (run_id,))
    if len(versions) > 1:
        out.append("\n**Cambios del plan:**\n")
        out.extend(f"- v{v['version']} (tras ronda {v['round_n']}): {v['reason']}" for v in versions)
    out.append("")

    errors = db.all("SELECT kind, subject, message, impact FROM errors WHERE run_id=? ORDER BY id", (run_id,))
    if errors:
        out.append("## Errores y resultados parciales\n")
        for e in errors[:40]:
            subj = f" `{e['subject'][:80]}`" if e["subject"] else ""
            out.append(f"- **{e['kind']}**{subj}: {e['message'][:200]}" + (f" → _{e['impact']}_" if e["impact"] else ""))
        if len(errors) > 40:
            out.append(f"- … y {len(errors) - 40} más (`ros errors {run_id}`)")
        out.append("")

    out.append("## Consumo\n")
    out.append(f"Tiempo {usage['elapsed_min']} min · coste ${usage['cost_usd']:.4f} · tokens "
               f"{usage['input_tokens']}+{usage['output_tokens']} · llamadas al modelo {usage['llm_calls']} · "
               f"búsquedas {usage['searches']} · descargas {usage['fetches']} · fuentes {usage['sources']}\n")
    return "\n".join(out)
