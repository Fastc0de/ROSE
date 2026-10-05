"""The `ros` command line end to end, offline (deterministic model) against an in-memory web."""

import pytest

from ros.cli import main
from ros.registry import build_app

from .test_monitor import FEED_A, LOREM, item, rss
from .test_research_engine import search_engine, site  # noqa: F401 (fixture)


@pytest.fixture
def ros(tmp_path, monkeypatch, fetcher, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ROS_HOME", str(tmp_path / ".ros"))
    monkeypatch.delenv("ROS_LLM", raising=False)
    monkeypatch.chdir(tmp_path)

    def run(*argv):
        code = main(["--offline", *argv], build_app=lambda s: build_app(s, search=search_engine(), fetcher=fetcher))
        out = capsys.readouterr()
        return code, out.out + out.err
    return run


def test_research_lifecycle(ros, site, tmp_path):  # noqa: F811
    code, out = ros("research", "baterías de sodio", "--max-rounds", "2", "--yes")
    assert code == 0 and "Interpretación" in out and "Estado: completed" in out
    assert ros("runs")[1].count("baterías de sodio") == 1
    code, out = ros("show", "1")
    assert "Ronda 1" in out and "Afirmaciones" in out and "Consumo" in out
    code, out = ros("report", "1", "-o", str(tmp_path / "r.md"))
    assert code == 0 and (tmp_path / "r.md").read_text().startswith("# ")
    assert ros("errors", "1")[0] == 0
    assert "Afirmaciones" in ros("search", "sodio")[1]
    code, out = ros("backup", str(tmp_path / "copia.db"))
    assert code == 0 and (tmp_path / "copia.db").exists()
    assert "ya terminó" in ros("resume", "1")[1]


def test_research_without_confirmation_in_batch_mode_is_refused(ros, site):  # noqa: F811
    code, out = ros("research", "baterías de sodio")
    assert code == 2 and "--yes" in out


def test_queue_and_cancel(ros, site):  # noqa: F811
    assert "En cola" in ros("research", "baterías de sodio", "--queue")[1]
    code, out = ros("cancel", "1")
    assert "cancelled" in out
    assert "cancelled" in ros("runs")[1]


def test_watch_lifecycle(ros, web):
    web.add(FEED_A, rss([item(1, "CATL lanza una batería de sodio", "CATL presenta celdas de sodio. " + LOREM * 2)]),
            content_type="application/rss+xml")
    code, out = ros("watch", "add", "sodio", "--source", f"rss:{FEED_A}", "--interest", "baterías de sodio",
                    "--every", "2h", "--digest", "daily@20:00", "--tz", "Europe/Madrid", "--yes", "--enable")
    assert code == 0 and "✔ rss:" in out and "Activado" in out
    assert "sodio" in ros("watch", "list")[1]
    code, out = ros("watch", "run", "sodio")
    assert code == 0 and "1 nuevos" in out
    assert "CATL" in ros("events", "sodio")[1]
    code, out = ros("digest", "sodio", "--now")
    assert "## Grupos temáticos" in out
    assert "No hay eventos nuevos" in ros("digest", "sodio", "--now")[1]
    assert "#1" in ros("digest", "sodio", "--list")[1]
    code, out = ros("inbox")
    assert "🗞" in out
    assert "Grupos temáticos" in ros("inbox", "--show", "1")[1]
    code, out = ros("watch", "edit", "sodio", "--every", "6h", "--yes")
    assert '"every_minutes": 360' in out and "versión 2" in out
    assert "v2" in ros("watch", "history", "sodio")[1]
    assert "Feedback #1" in ros("feedback", "event", "1", "more")[1]
    assert "blog.example" in ros("sources")[1]
    assert ros("prune", "--older-than", "30", "--dry-run")[0] == 0
    code, out = ros("connectors")
    assert "youtube_api" in out and "unconfigured" in out
    assert "Ciclo completado" in ros("daemon", "--once")[1]
    assert "Desactivado" in ros("watch", "disable", "sodio")[1]


def test_draft_and_research_to_watch(ros, site, web):  # noqa: F811
    web.add(FEED_A, rss([item(1, "Nada", "texto " + LOREM)]), content_type="application/rss+xml")
    code, out = ros("draft", f"Revisa {FEED_A} cada dos horas y dame un informe a las 9:30", "--name", "blog", "--yes")
    assert code == 0 and "cada 2 h" in out and "09:30" in out and "guardado" in out
    code, out = ros("draft", "Sigue estas cuentas y detecta lanzamientos")
    assert code == 2 and "incompleto" in out
    ros("research", "baterías de sodio", "--max-rounds", "1", "--yes")
    code, out = ros("watch", "from-research", "1", "--name", "desde-inv", "--yes")
    assert code == 0 and "search: baterías de sodio" in out and "guardado" in out
