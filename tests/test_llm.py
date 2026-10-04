from types import SimpleNamespace

import pytest

from ros.budget import Budget, Ledger
from ros.db import now_iso
from ros.errors import BudgetExhausted, ErrorKind, RosError
from ros.llm import AnthropicLLM, FakeLLM, price

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"],
          "additionalProperties": False}


@pytest.fixture
def ledger(db):
    ts = now_iso()
    run = db.insert("runs", {"kind": "research", "objective": "x", "status": "running", "created_at": ts,
                             "updated_at": ts})
    return Ledger(db, run, Budget(max_cost_usd=1.0, max_tokens=100_000, final_reserve=0.0))


def test_fake_llm_usage_is_recorded(ledger):
    llm = FakeLLM(lambda *a: {"ok": True})
    assert llm.complete_json(purpose="t", system="s", user="u", schema=SCHEMA, max_tokens=100, ledger=ledger) == {"ok": True}
    totals = ledger.totals()
    assert totals.llm_calls == 1 and totals.input_tokens > 0 and totals.cost_usd > 0


def test_call_is_not_made_when_budget_cannot_cover_worst_case(ledger):
    llm = FakeLLM(lambda *a: {"ok": True})
    with pytest.raises(BudgetExhausted):
        llm.complete_json(purpose="t", system="s", user="u", schema=SCHEMA, max_tokens=200_000, ledger=ledger)
    assert llm.calls == []


class _Stream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


def _message(text='{"ok": true}', stop_reason="end_turn", model="claude-opus-5-5"):
    usage = SimpleNamespace(input_tokens=1000, output_tokens=500, cache_creation_input_tokens=0,
                            cache_read_input_tokens=0)
    content = [SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(usage=usage, stop_reason=stop_reason, content=content, model=model)


def _claude(message, calls):
    llm = AnthropicLLM(model="claude-opus-5-5", effort="medium", api_key="test-key")

    def stream(**kwargs):
        calls.append(kwargs)
        return _Stream(message)

    llm.client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)))
    return llm


def test_claude_request_shape_and_billing(ledger):
    calls = []
    llm = _claude(_message(), calls)
    assert llm.complete_json(purpose="plan", system="s", user="u", schema=SCHEMA, max_tokens=8000, ledger=ledger) == {"ok": True}
    kwargs = calls[0]
    assert kwargs["output_config"] == {"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}}
    assert kwargs["fallbacks"] == "default" and kwargs["betas"] == ["server-side-fallback-2026-07-01"]
    assert "extra_body" not in kwargs
    in_p, out_p = price("claude-opus-5-5")
    assert ledger.totals().cost_usd == pytest.approx((1000 * in_p + 500 * out_p) / 1e6)


def test_fallback_model_is_billed_at_its_own_price(ledger):
    llm = _claude(_message(model="claude-opus-5"), [])
    llm.complete_json(purpose="plan", system="s", user="u", schema=SCHEMA, max_tokens=8000, ledger=ledger)
    row = ledger.db.one("SELECT model, cost_usd FROM usage WHERE kind='llm'")
    in_p, out_p = price("claude-opus-5")
    assert row["model"] == "claude-opus-5" and row["cost_usd"] == pytest.approx((1000 * in_p + 500 * out_p) / 1e6)


@pytest.mark.parametrize("stop_reason,text,kind", [
    ("refusal", "", ErrorKind.MODEL_ERROR),
    ("max_tokens", '{"ok": tr', ErrorKind.EXTRACTION_INCOMPLETE),
    ("end_turn", "   ", ErrorKind.EMPTY_RESPONSE),
    ("end_turn", "not json", ErrorKind.MODEL_ERROR),
])
def test_failed_calls_raise_typed_errors_and_still_bill(ledger, stop_reason, text, kind):
    llm = _claude(_message(text=text, stop_reason=stop_reason), [])
    with pytest.raises(RosError) as info:
        llm.complete_json(purpose="extract", system="s", user="u", schema=SCHEMA, max_tokens=8000, ledger=ledger)
    assert info.value.kind == kind
    row = ledger.db.one("SELECT purpose, output_tokens FROM usage WHERE kind='llm'")
    assert row["purpose"] == "extract:failed" and row["output_tokens"] == 500
