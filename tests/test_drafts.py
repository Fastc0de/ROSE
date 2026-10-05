"""Natural-language configuration: the four example instructions from specs.txt (offline parser)."""

import pytest

from ros.connectors.search import StaticSearchBackend
from ros.monitor.drafts import diff_specs, draft_from_text
from ros.monitor.spec import SourceRef
from ros.offline import offline_handler


@pytest.fixture
def app(make_app):
    return make_app(offline_handler, StaticSearchBackend(lambda q, n: []))


def test_research_instruction_with_focus(app, db):
    d = draft_from_text(app, "Investiga esta empresa y presta especial atención a problemas legales, opiniones de "
                             "empleados y señales de dificultades financieras.")
    assert d.kind == "research" and d.spec is None
    assert "problemas legales" in d.research["focus"]
    assert d.research["budget"]["max_rounds"] == 4
    assert db.one("SELECT kind, status FROM config_drafts")["kind"] == "research"


def test_cadence_and_end_of_day_digest(app):
    d = draft_from_text(app, "Revisa estas fuentes cada cuatro horas, pero entrégame un solo informe al final del día.",
                        extra_sources=[SourceRef(kind="rss", locator="https://blog.example/feed")])
    assert d.kind == "watch" and d.spec is not None
    assert d.spec.every_minutes == 240
    assert d.spec.digest.mode == "daily" and d.spec.digest.at == "21:00"
    assert any("21:00" in a for a in d.assumptions)
    assert "Confirmar el trabajo recurrente antes de activarlo." in d.requires_user_action


def test_immediate_alert_rules(app):
    d = draft_from_text(app, "Avísame inmediatamente si aparece una vulnerabilidad crítica o si varias fuentes "
                             "independientes reportan el mismo problema.",
                        extra_sources=[SourceRef(kind="search", locator="vulnerabilidad crítica")])
    a = d.spec.alerts
    assert a.enabled and a.min_independent_sources == 2
    assert a.keywords == ["vulnerabilidad crítica"]


def test_accounts_not_given_are_reported_as_unresolved(app):
    text = ("Sigue estas cuentas y detecta lanzamientos, colaboraciones, cambios de estrategia y reacciones "
            "negativas de los usuarios.")
    d = draft_from_text(app, text)
    assert d.spec is None and not d.complete
    assert any("fuente" in u.lower() for u in d.unresolved)
    assert d.requires_user_action
    # Once the user supplies the accounts, the same text yields a valid, editable spec.
    d2 = draft_from_text(app, text, extra_sources=[SourceRef(kind="youtube", locator="@marca")], name="marca")
    assert d2.spec.name == "marca"
    assert d2.spec.topics[:2] == ["lanzamientos", "colaboraciones"]
    assert "reacciones negativas de los usuarios" in d2.spec.topics


def test_unconfigured_platform_requires_user_action(app):
    d = draft_from_text(app, "Sigue la cuenta @marca de Instagram y avísame de lanzamientos.")
    assert d.spec.sources[0].kind == "instagram"
    assert any("unconfigured" in r for r in d.requires_user_action)


def test_edit_by_text_keeps_the_rest_and_shows_a_diff(app):
    base = draft_from_text(app, "Revisa https://blog.example/feed cada dos horas.", name="blog").spec
    edited = draft_from_text(app, "Revisa https://blog.example/feed cada seis horas.", current=base).spec
    assert edited.name == "blog" and edited.every_minutes == 360
    diff = diff_specs(base, edited)
    assert '-  "every_minutes": 120' in diff and '+  "every_minutes": 360' in diff
