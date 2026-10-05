import json
from types import SimpleNamespace

import pytest

from ros.budget import Budget, Ledger
from ros.db import now_iso
from ros.errors import BudgetExhausted, ErrorKind, RosError
from ros.llm import PRICING, AnthropicLLM, FakeLLM, price

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


def test_output_ceiling_shrinks_to_what_the_budget_can_pay(ledger):
    calls = []
    llm = _claude(_message(), calls)        # Opus: $20 per million output tokens
    ledger.budget.max_cost_usd = 0.10      # pays for ~5,000 output tokens, not the 12,000 requested
    llm.complete_json(purpose="plan", system="s", user="u", schema=SCHEMA, max_tokens=12_000, ledger=ledger)
    assert 3_000 <= calls[0]["max_tokens"] < 5_000


def test_call_is_not_made_when_budget_cannot_cover_a_useful_answer(ledger):
    llm = FakeLLM(lambda *a: {"ok": True})  # Haiku: $5 per million output tokens
    ledger.budget.max_cost_usd = 0.01      # ~2,000 output tokens: below the useful floor
    with pytest.raises(BudgetExhausted):
        llm.complete_json(purpose="t", system="s", user="u", schema=SCHEMA, max_tokens=12_000, ledger=ledger)
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


def _claude(message, calls, model="claude-opus-5-5", effort="medium"):
    llm = AnthropicLLM(model=model, effort=effort, api_key="test-key")

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


def test_haiku_gets_no_effort_and_no_fallbacks(ledger):
    calls = []
    llm = _claude(_message(model="claude-haiku-4-5"), calls, model="claude-haiku-4-5", effort="high")
    llm.complete_json(purpose="extract", system="s", user="u", schema=SCHEMA, max_tokens=8000, ledger=ledger)
    assert calls[0]["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}}
    assert "fallbacks" not in calls[0] and "betas" not in calls[0]


def test_workspace_header_is_sent():
    llm = AnthropicLLM(model="claude-sonnet-5-5", api_key="test-key", workspace_id="wrkspc_123")
    assert llm.client.default_headers["anthropic-workspace-id"] == "wrkspc_123"
    shared = AnthropicLLM(model="claude-haiku-4-5", client=llm.client)
    assert shared.client is llm.client


def test_missing_workspace_is_reported_clearly(ledger):
    import anthropic
    import httpx2

    llm = AnthropicLLM(model="claude-opus-5-5", api_key="test-key")
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    body = {"type": "error", "error": {"type": "invalid_request_error",
                                       "message": "This API key is not scoped to a workspace"}}
    error = anthropic.BadRequestError("bad", response=httpx2.Response(400, request=request, json=body), body=body)

    def stream(**kwargs):
        raise error

    llm.client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)))
    with pytest.raises(RosError) as info:
        llm.complete_json(purpose="plan", system="s", user="u", schema=SCHEMA, max_tokens=100, ledger=ledger)
    assert info.value.kind == ErrorKind.INVALID_CREDENTIALS and "ANTHROPIC_WORKSPACE_ID" in info.value.message


# -- OpenRouter -----------------------------------------------------------

def _openrouter(handler, **kw):
    import httpx

    from ros.llm import OpenRouterLLM
    OpenRouterLLM._catalog.clear()
    return OpenRouterLLM("spastealth/space-bunny-alpha", api_key="sk-or-test",
                         client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None, **kw)


OR_SCHEMA = {"type": "object", "properties": {"title": {"type": "string"},
                                            "kind": {"type": "string", "enum": ["fact", "rumor"]},
                                            "items": {"type": "array", "items": {"type": "string"}},
                                            "score": {"type": "number"}},
          "required": ["title", "kind", "items", "score"], "additionalProperties": False}


def _completion(content, usage=None, finish="stop"):
    import httpx
    return httpx.Response(200, json={"model": "spastealth/space-bunny-alpha",
                                     "choices": [{"message": {"content": content}, "finish_reason": finish}],
                                     "usage": usage or {"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.0012}})


def test_openrouter_structured_call_bills_reported_cost(db):
    import httpx
    sent = []

    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "spastealth/space-bunny-alpha",
                                                       "pricing": {"prompt": "0.000001", "completion": "0.000002"}}]})
        sent.append(json.loads(request.content))
        return _completion('{"title": "t", "kind": "fact", "items": ["a"], "score": 0.5}')

    llm = _openrouter(handler)
    ts = now_iso()
    run = db.insert("runs", {"kind": "research", "objective": "x", "status": "running", "created_at": ts,
                             "updated_at": ts})
    ledger = Ledger(db, run, Budget(max_cost_usd=1.0, final_reserve=0.0))
    data = llm.complete_json(purpose="extract", system="s", user="u", schema=OR_SCHEMA, max_tokens=4000, ledger=ledger)
    assert data == {"title": "t", "kind": "fact", "items": ["a"], "score": 0.5}
    body = sent[0]
    assert body["model"] == "spastealth/space-bunny-alpha" and body["max_tokens"] == 4000
    assert body["response_format"]["json_schema"]["schema"] == OR_SCHEMA and body["usage"] == {"include": True}
    assert llm.price_of("spastealth/space-bunny-alpha") == pytest.approx((1.0, 2.0))
    row = db.one("SELECT model, input_tokens, output_tokens, cost_usd FROM usage")
    assert (row["model"], row["input_tokens"], row["output_tokens"]) == ("spastealth/space-bunny-alpha", 100, 20)
    assert row["cost_usd"] == pytest.approx(0.0012)


def test_openrouter_lenient_parsing_and_schema_fallback():
    import httpx
    calls = []

    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(500)
        body = json.loads(request.content)
        calls.append(body)
        if "response_format" in body:
            return httpx.Response(404, json={"error": {"code": 404,
                                                       "message": "No endpoints found that support response_format"}})
        return _completion('Aquí está:\n```json\n{"title": "x", "kind": "otro", "items": "a", "score": "0.7"}\n```')

    llm = _openrouter(handler)
    data = llm.complete_json(purpose="plan", system="s", user="u", schema=OR_SCHEMA, max_tokens=4000, ledger=None)
    assert data == {"title": "x", "kind": "fact", "items": ["a"], "score": 0.7}   # conformed to the schema
    assert "JSON schema" in calls[1]["messages"][0]["content"]
    assert llm.price_of("spastealth/space-bunny-alpha") == max(PRICING.values())  # unknown price: conservative


@pytest.mark.parametrize("status,kind", [(401, ErrorKind.INVALID_CREDENTIALS), (402, ErrorKind.NO_BALANCE),
                                         (400, ErrorKind.MODEL_ERROR)])
def test_openrouter_errors_are_typed(status, kind):
    import httpx
    llm = _openrouter(lambda r: httpx.Response(status, json={"error": {"code": status, "message": "nope"}}))
    with pytest.raises(RosError) as info:
        llm.complete_json(purpose="plan", system="s", user="u", schema=OR_SCHEMA, max_tokens=4000, ledger=None)
    assert info.value.kind == kind


def test_openrouter_retries_transient_errors_and_reports_truncation():
    import httpx
    state = {"n": 0}

    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(500)
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(503, json={"error": {"code": 503, "message": "busy"}})
        return _completion('{"title": "t"', finish="length")

    llm = _openrouter(handler)
    with pytest.raises(RosError) as info:
        llm.complete_json(purpose="plan", system="s", user="u", schema=OR_SCHEMA, max_tokens=4000, ledger=None)
    assert info.value.kind == ErrorKind.EXTRACTION_INCOMPLETE and state["n"] == 2


def test_openrouter_requires_a_key():
    from ros.llm import OpenRouterLLM
    with pytest.raises(RosError) as info:
        OpenRouterLLM("m", api_key="")
    assert info.value.kind == ErrorKind.INVALID_CREDENTIALS


def test_openrouter_never_guesses_missing_required_fields(db):
    import httpx
    answers = ['{"title": "t", "kind": "fact", "items": []}',                       # no score: retried
               '{"title": "t", "kind": "fact", "items": [], "score": 1}']
    bodies = []

    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(500)
        bodies.append(json.loads(request.content))
        return _completion(answers.pop(0))

    llm = _openrouter(handler)
    assert llm.complete_json(purpose="triage", system="s", user="u", schema=OR_SCHEMA, max_tokens=4000,
                             ledger=None)["score"] == 1.0
    assert "Every key in the schema is required" in bodies[1]["messages"][0]["content"]

    answers[:] = ['{"title": "t"}', '{"title": "t"}']
    ts = now_iso()
    run = db.insert("runs", {"kind": "research", "objective": "x", "status": "running", "created_at": ts,
                             "updated_at": ts})
    ledger = Ledger(db, run, Budget(max_cost_usd=1.0, final_reserve=0.0))
    with pytest.raises(RosError) as info:
        llm.complete_json(purpose="triage", system="s", user="u", schema=OR_SCHEMA, max_tokens=4000, ledger=ledger)
    assert info.value.kind == ErrorKind.MODEL_ERROR and "faltan" in info.value.message
    row = db.one("SELECT purpose, input_tokens, cost_usd FROM usage")
    assert row["purpose"] == "triage:failed" and row["input_tokens"] == 200 and row["cost_usd"] == pytest.approx(0.0024)


# -- OpenCode -------------------------------------------------------------

def _opencode(handler):
    import httpx

    from ros.llm import OpenCodeLLM
    return OpenCodeLLM("glm-5.3", api_key="oc_sk_test", client=httpx.Client(transport=httpx.MockTransport(handler)),
                       sleep=lambda s: None)


def test_opencode_identifies_client_and_session_per_run(db):
    import httpx
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"model": "glm-5.3", "choices": [{"finish_reason": "stop", "message": {
            "content": '{"title": "t", "kind": "rumor", "items": [], "score": 1}', "reasoning_content": "…"}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500}})

    llm = _opencode(handler)
    ts = now_iso()
    run = db.insert("runs", {"kind": "research", "objective": "x", "status": "running", "created_at": ts,
                             "updated_at": ts})
    ledger = Ledger(db, run, Budget(max_cost_usd=1.0, final_reserve=0.0))
    assert llm.complete_json(purpose="plan", system="s", user="u", schema=OR_SCHEMA, max_tokens=4000,
                             ledger=ledger)["kind"] == "rumor"
    req = seen[0]
    assert str(req.url) == "https://opencode.ai/zen/go/v1/chat/completions"
    assert req.headers["x-opencode-session"] == f"ros-run-{run}"
    assert req.headers["user-agent"].startswith("ros-research/")
    assert "usage" not in json.loads(req.content)                      # OpenRouter-only field not sent
    cost = db.one("SELECT cost_usd FROM usage")["cost_usd"]
    assert cost == pytest.approx((1000 * 1.40 + 500 * 4.40) / 1_000_000)   # GLM-5.3 Go price


def test_opencode_account_settings_error_is_fatal():
    import httpx
    llm = _opencode(lambda r: httpx.Response(400, json={"error": {"type": "server_error", "message":
        "Upstream request failed: This Go model requires Global regions. Select Global in your workspace's "
        "Privacy settings to use it."}}))
    with pytest.raises(RosError) as info:
        llm.complete_json(purpose="extract", system="s", user="u", schema=OR_SCHEMA, max_tokens=4000, ledger=None)
    assert info.value.fatal and "Privacy settings" in info.value.message
