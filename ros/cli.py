"""Command-line entry point: `ros`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import load_settings
from .errors import RosError

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_INTERRUPTED = 0, 1, 2, 130


def _echo(message: str) -> None:
    print(message, flush=True)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ros", description="Investigación profunda y monitorización de fuentes.")
    p.add_argument("--version", action="version", version=f"ros {__version__}")
    p.add_argument("--config", help="archivo TOML de configuración")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("research", help="investigar un tema en varias rondas")
    r.add_argument("objective", help="qué quieres investigar, en lenguaje natural")
    r.add_argument("--focus", default="", help="aspectos a los que prestar especial atención")
    r.add_argument("--exclude", action="append", default=[], metavar="DOMINIO", help="dominio a excluir (repetible)")
    r.add_argument("--max-rounds", type=int)
    r.add_argument("--max-sources", type=int)
    r.add_argument("--max-minutes", type=float)
    r.add_argument("--max-cost", type=float, help="coste máximo en USD")
    r.add_argument("--offline", action="store_true", help="sin modelo de lenguaje (demo, sin coste ni análisis real)")
    r.add_argument("-o", "--output", help="dónde guardar el informe Markdown")
    return p


def _budget_overrides(args: argparse.Namespace) -> dict:
    pairs = {"max_rounds": args.max_rounds, "max_sources": args.max_sources,
             "max_minutes": args.max_minutes, "max_cost_usd": args.max_cost}
    return {k: v for k, v in pairs.items() if v is not None}


def cmd_research(args: argparse.Namespace, build_app=None) -> int:
    from .registry import build_app as default_build
    from .research.engine import ResearchEngine

    overrides: dict = {"llm": "fake" if args.offline else None}
    if budget := _budget_overrides(args):
        overrides["budget"] = budget
    settings = load_settings(args.config, overrides)
    app = (build_app or default_build)(settings)
    engine = ResearchEngine(app, echo=_echo)
    run_id = engine.create(args.objective, focus=args.focus, exclude_domains=tuple(args.exclude))
    _echo(f"Investigación #{run_id}")
    try:
        status = engine.run(run_id)
    except KeyboardInterrupt:
        _echo(f"\nPausada. El progreso está guardado (investigación #{run_id}).")
        return EXIT_INTERRUPTED
    report = app.db.one("SELECT report_md FROM runs WHERE id=?", (run_id,))["report_md"]
    if report:
        path = Path(args.output) if args.output else Path(settings.reports_dir) / f"research-{run_id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report, encoding="utf-8")
        _echo(f"\nEstado: {status}. Informe: {path}")
    else:
        _echo(f"\nEstado: {status}. No se generó informe.")
    return EXIT_FAILED if status == "failed" else EXIT_OK


COMMANDS = {"research": cmd_research}


def main(argv: list[str] | None = None, *, build_app=None) -> int:
    """`build_app(settings) -> App` can be injected (tests use fakes for network and model)."""
    args = _parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args, build_app)
    except (RosError, ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
