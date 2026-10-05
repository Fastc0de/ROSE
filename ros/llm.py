"""Language-model port. Every call returns schema-constrained JSON and is metered.

The model never chooses tools or touches configuration: it only returns data that
the calling code validates and applies within hard limits.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from .budget import Ledger
from .errors import ErrorKind, RosError

# USD per million tokens (input, output). Unknown models fall back to the most expensive row
# so that budget reservations stay conservative.
PRICING: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1"}
# Smallest output ceiling worth a call: below this a structured answer (plus reasoning) rarely fits.
MIN_OUTPUT_TOKENS = 3_000
# Models that reject `output_config.effort` with a 400.
NO_EFFORT_MODELS = {"claude-haiku-4-5"}


def price(model: str) -> tuple[float, float]:
    return PRICING.get(model, max(PRICING.values()))


def estimate_tokens(text: str) -> int:
    return int(len(text) / 3.2) + 16


def obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    """JSON-schema object helper: closed, every property required unless stated."""
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required, "additionalProperties": False}


def arr(items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": items}


STR = {"type": "string"}
NUM = {"type": "number"}
INT = {"type": "integer"}
BOOL = {"type": "boolean"}


def enum(*values: str) -> dict[str, Any]:
    return {"type": "string", "enum": list(values)}


class LLM:
    model: str = "unknown"

    def price_of(self, model: str) -> tuple[float, float]:
        """USD per million (input, output) tokens. Providers with live price lists override this."""
        return price(model)

    def complete_json(self, *, purpose: str, system: str, user: str, schema: dict, max_tokens: int,
                      ledger: Ledger | None) -> dict:
        in_price, out_price = self.price_of(self.model)
        est_in = estimate_tokens(system) + estimate_tokens(user)
        est_cost = (est_in * in_price + max_tokens * out_price) / 1_000_000
        if ledger:
            # Shrink the output ceiling to what the remaining budget can pay for (never below a useful floor).
            max_tokens, est_cost = ledger.grant_output(est_in, max_tokens, min(MIN_OUTPUT_TOKENS, max_tokens),
                                                       in_price, out_price)
        in_tok = out_tok = 0
        served = self.model
        billed: float | None = None
        try:
            result = self._call(purpose=purpose, system=system, user=user, schema=schema, max_tokens=max_tokens)
            data, in_tok, out_tok, served = result[:4]
            billed = result[4] if len(result) > 4 else None   # provider-reported cost, when available
            return data
        finally:
            if ledger:
                # A server-side fallback may have answered with another model: bill at its price.
                s_in, s_out = self.price_of(served)
                cost = billed if billed is not None else (in_tok * s_in + out_tok * s_out) / 1_000_000
                ledger.settle_llm(est_in, max_tokens, est_cost, purpose=purpose, model=served,
                                  input_tokens=in_tok, output_tokens=out_tok, cost=cost)

    def _call(self, *, purpose: str, system: str, user: str, schema: dict, max_tokens: int
              ) -> tuple[dict, int, int, str]:
        """Returns (data, input_tokens, output_tokens, model_that_served_the_request)."""
        raise NotImplementedError


class AnthropicLLM(LLM):
    def __init__(self, model: str = "claude-opus-5-5", effort: str = "medium", api_key: str | None = None,
                 workspace_id: str = "", client: Any = None):
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover
            raise RosError(ErrorKind.INTERNAL, "the 'anthropic' package is not installed") from exc
        self._anthropic = anthropic
        self.model = model
        self.effort = "" if model in NO_EFFORT_MODELS else effort
        if client is not None:
            self.client = client
            return
        headers = {"anthropic-workspace-id": workspace_id} if workspace_id else None
        try:
            self.client = anthropic.Anthropic(api_key=api_key, max_retries=3, default_headers=headers)
        except anthropic.AnthropicError as exc:
            raise RosError(ErrorKind.INVALID_CREDENTIALS,
                           "no Anthropic credentials: set ANTHROPIC_API_KEY (or run `ant auth login`)") from exc

    def _call(self, *, purpose: str, system: str, user: str, schema: dict, max_tokens: int
              ) -> tuple[dict, int, int, str]:
        a = self._anthropic
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
        if self.effort:
            output_config["effort"] = self.effort
        kwargs: dict[str, Any] = {}
        if self.model in FALLBACK_MODELS:
            # On a safety refusal the API re-runs the request on a fallback model chosen by category.
            kwargs.update(fallbacks="default", betas=["server-side-fallback-2026-07-01"])
        try:
            with self.client.beta.messages.stream(
                model=self.model, max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": user}],
                output_config=output_config,
                **kwargs,
            ) as stream:
                msg = stream.get_final_message()
        except a.AuthenticationError as exc:
            raise RosError(ErrorKind.INVALID_CREDENTIALS, f"Anthropic rejected the API key: {exc}") from exc
        except a.PermissionDeniedError as exc:
            raise RosError(ErrorKind.INVALID_CREDENTIALS, f"permission denied for model {self.model}: {exc}") from exc
        except a.BadRequestError as exc:
            text = f"{exc} {getattr(exc, 'body', '')}".lower()
            if "workspace" in text:
                raise RosError(ErrorKind.INVALID_CREDENTIALS, "la API key no está asociada a un workspace: "
                               "define ANTHROPIC_WORKSPACE_ID (o anthropic_workspace_id en ros.toml)") from exc
            kind = ErrorKind.NO_BALANCE if "credit balance" in text or "billing" in text else ErrorKind.MODEL_ERROR
            raise RosError(kind, f"model request rejected: {exc}") from exc
        except a.RateLimitError as exc:
            raise RosError(ErrorKind.RATE_LIMITED, f"model rate limit: {exc}") from exc
        except (a.APITimeoutError, a.APIConnectionError, a.InternalServerError) as exc:
            raise RosError(ErrorKind.TRANSIENT, f"model temporarily unavailable: {exc}") from exc
        except a.APIStatusError as exc:
            raise RosError(ErrorKind.MODEL_ERROR, f"model error: {exc}") from exc

        usage = msg.usage
        in_tok = (usage.input_tokens or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0) \
            + (getattr(usage, "cache_read_input_tokens", 0) or 0)
        out_tok = usage.output_tokens or 0
        served = getattr(msg, "model", None) or self.model
        if msg.stop_reason == "refusal":
            raise _UsageCarrier(RosError(ErrorKind.MODEL_ERROR, f"model declined the {purpose} request"), in_tok, out_tok, served)
        if msg.stop_reason == "max_tokens":
            raise _UsageCarrier(RosError(ErrorKind.EXTRACTION_INCOMPLETE,
                                         f"model output for {purpose} hit max_tokens={max_tokens}"), in_tok, out_tok, served)
        text = "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text")
        if not text.strip():
            raise _UsageCarrier(RosError(ErrorKind.EMPTY_RESPONSE, f"empty model response for {purpose}"), in_tok, out_tok, served)
        try:
            return json.loads(text), in_tok, out_tok, served
        except json.JSONDecodeError as exc:
            raise _UsageCarrier(RosError(ErrorKind.MODEL_ERROR, f"invalid JSON from model for {purpose}"),
                                in_tok, out_tok, served) from exc

    def complete_json(self, **kwargs: Any) -> dict:
        return _billing_failures(self, super().complete_json, kwargs)


def _billing_failures(llm: LLM, call: Callable[..., dict], kwargs: dict) -> dict:
    """Unwrap usage-carrying errors so that tokens spent on failed calls are still billed to the ledger."""
    ledger: Ledger | None = kwargs.get("ledger")
    try:
        return call(**kwargs)
    except _UsageCarrier as carrier:
        if ledger:
            in_price, out_price = llm.price_of(carrier.model)
            cost = carrier.cost if carrier.cost is not None else \
                (carrier.in_tok * in_price + carrier.out_tok * out_price) / 1_000_000
            ledger.record("llm", purpose=kwargs["purpose"] + ":failed", model=carrier.model,
                          input_tokens=carrier.in_tok, output_tokens=carrier.out_tok, cost_usd=cost)
        raise carrier.error from None


class OpenRouterLLM(LLM):
    """Any model on OpenRouter (OpenAI-compatible chat completions) with JSON-schema outputs.

    Structured output is requested with `response_format: json_schema`. Models that ignore it still
    get the schema in the prompt; the answer is parsed leniently and conformed to the schema, so the
    pipeline never sees a missing key. Prices come from OpenRouter's model list (fetched once); if
    that fails the most expensive known price is reserved, keeping budgets conservative. The cost
    OpenRouter reports for each call is what gets billed to the ledger.
    """

    _catalog: dict[str, dict[str, tuple[float, float]]] = {}

    def __init__(self, model: str, *, api_key: str | None, base_url: str = "https://openrouter.ai/api/v1",
                 client: Any = None, max_retries: int = 3, sleep: Callable[[float], None] | None = None):
        import time

        import httpx
        if not api_key:
            raise RosError(ErrorKind.INVALID_CREDENTIALS,
                           "falta la clave de OpenRouter: define OPENROUTER_API_KEY (o openrouter_api_key_env)")
        self._httpx = httpx
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=httpx.Timeout(300.0, connect=20.0))
        self.headers = {"Authorization": f"Bearer {api_key}", "HTTP-Referer": "https://github.com/Fastc0de/ROSE",
                        "X-Title": "ROS"}
        self.max_retries = max_retries
        self.sleep = sleep or time.sleep

    # -- prices -------------------------------------------------------------
    def price_of(self, model: str) -> tuple[float, float]:
        catalog = self._catalog.get(self.base_url)
        if catalog is None:
            catalog = {}
            try:
                resp = self.client.get(f"{self.base_url}/models", headers=self.headers, timeout=30.0)
                for m in resp.json().get("data", []) if resp.status_code == 200 else []:
                    p = m.get("pricing") or {}
                    try:
                        catalog[m["id"]] = (float(p.get("prompt", 0)) * 1_000_000,
                                            float(p.get("completion", 0)) * 1_000_000)
                    except (TypeError, ValueError):
                        continue
            except Exception:  # noqa: BLE001 - pricing is best effort; reservations fall back to the max
                pass
            self._catalog[self.base_url] = catalog
        return catalog.get(model) or price(model)

    # -- calls --------------------------------------------------------------
    def _call(self, *, purpose: str, system: str, user: str, schema: dict, max_tokens: int
              ) -> tuple[dict, int, int, str, float | None]:
        instruction = ("\n\nRespond with a single JSON object that matches the requested schema. No prose, no "
                       "markdown fences.")
        body: dict[str, Any] = {
            "model": self.model, "max_tokens": max_tokens, "usage": {"include": True},
            "messages": [{"role": "system", "content": system + instruction}, {"role": "user", "content": user}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": re.sub(r"[^A-Za-z0-9_-]", "_", purpose)[:64],
                                                "strict": True, "schema": schema}},
        }
        try:
            msg = self._post(body)
        except RosError as exc:
            if exc.kind != ErrorKind.MODEL_ERROR or "response_format" not in exc.message.lower() \
                    and "structured" not in exc.message.lower() and "no endpoints" not in exc.message.lower():
                raise
            # The model cannot do structured outputs: put the schema in the prompt instead.
            body.pop("response_format")
            body["messages"][0]["content"] = (system + instruction + "\nJSON schema:\n"
                                              + json.dumps(schema, ensure_ascii=False))
            msg = self._post(body)
        usage = msg.get("usage") or {}
        in_tok, out_tok = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
        cost = usage.get("cost")
        cost = float(cost) if isinstance(cost, (int, float)) else None
        served = msg.get("model") or self.model
        choice = (msg.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        if isinstance(text, list):  # some providers return content parts
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        finish = choice.get("finish_reason") or choice.get("native_finish_reason")
        if message.get("refusal"):
            raise _UsageCarrier(RosError(ErrorKind.MODEL_ERROR, f"model declined the {purpose} request"),
                                in_tok, out_tok, served, cost)
        if finish == "length":
            raise _UsageCarrier(RosError(ErrorKind.EXTRACTION_INCOMPLETE,
                                         f"model output for {purpose} hit max_tokens={max_tokens}"),
                                in_tok, out_tok, served, cost)
        if not text.strip():
            raise _UsageCarrier(RosError(ErrorKind.EMPTY_RESPONSE, f"empty model response for {purpose}"),
                                in_tok, out_tok, served, cost)
        data = parse_json_object(text)
        if data is None:
            raise _UsageCarrier(RosError(ErrorKind.MODEL_ERROR, f"invalid JSON from model for {purpose}"),
                                in_tok, out_tok, served, cost)
        return conform(data, schema), in_tok, out_tok, served, cost

    def complete_json(self, **kwargs: Any) -> dict:
        return _billing_failures(self, super().complete_json, kwargs)

    def _post(self, body: dict) -> dict:
        attempt = 0
        while True:
            try:
                return self._post_once(body)
            except RosError as exc:
                if not exc.retryable or attempt >= self.max_retries:
                    raise
                attempt += 1
                self.sleep(min(exc.retry_after or 2 ** attempt, 60))

    def _post_once(self, body: dict) -> dict:
        httpx = self._httpx
        try:
            resp = self.client.post(f"{self.base_url}/chat/completions", json=body, headers=self.headers)
        except httpx.TimeoutException as exc:
            raise RosError(ErrorKind.TRANSIENT, "OpenRouter: timeout") from exc
        except httpx.TransportError as exc:
            raise RosError(ErrorKind.TRANSIENT, f"OpenRouter: {exc}") from exc
        try:
            data = resp.json()
        except ValueError:
            data = {}
        error = data.get("error") if isinstance(data, dict) else None
        code = resp.status_code
        if code < 400 and not error:
            return data
        if error and code < 400:
            code = int(error.get("code") or 500) if str(error.get("code", "")).isdigit() else 500
        detail = (error or {}).get("message") or resp.text[:300]
        meta = (error or {}).get("metadata") or {}
        if meta.get("raw"):
            detail += f" ({str(meta['raw'])[:200]})"
        if code == 401:
            raise RosError(ErrorKind.INVALID_CREDENTIALS, f"OpenRouter rechazó la clave: {detail}")
        if code == 402:
            raise RosError(ErrorKind.NO_BALANCE, f"OpenRouter: saldo insuficiente: {detail}")
        if code == 429:
            retry = resp.headers.get("retry-after")
            raise RosError(ErrorKind.RATE_LIMITED, f"OpenRouter: límite de uso: {detail}",
                           retry_after=float(retry) if retry and retry.replace(".", "", 1).isdigit() else None)
        if code in (408, 502, 503, 504) or code >= 500:
            raise RosError(ErrorKind.TRANSIENT, f"OpenRouter no disponible ({code}): {detail}")
        raise RosError(ErrorKind.MODEL_ERROR, f"OpenRouter rechazó la petición ({code}): {detail}")


def parse_json_object(text: str) -> dict | None:
    """The JSON object in a model answer, tolerating code fences and text around it."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    candidates = [text, fenced.group(1) if fenced else None]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for c in candidates:
        if not c:
            continue
        try:
            value = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def conform(value: Any, schema: dict) -> Any:
    """Coerce a decoded answer to the schema: missing keys get empty defaults, enums stay valid."""
    kind = schema.get("type")
    if kind == "object":
        value = value if isinstance(value, dict) else {}
        props = schema.get("properties", {})
        out = {k: v for k, v in value.items() if k in props or not schema.get("additionalProperties") is False}
        for key, sub in props.items():
            out[key] = conform(value.get(key), sub)
        return out
    if kind == "array":
        if not isinstance(value, list):
            value = [] if value is None else [value]
        return [conform(v, schema.get("items", {})) for v in value]
    if "enum" in schema:
        return value if value in schema["enum"] else schema["enum"][0]
    if kind == "string":
        return "" if value is None else value if isinstance(value, str) else json.dumps(value, ensure_ascii=False) \
            if isinstance(value, (dict, list)) else str(value)
    if kind in ("number", "integer"):
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = 0.0
        return int(number) if kind == "integer" else number
    if kind == "boolean":
        return value if isinstance(value, bool) else str(value).strip().lower() in ("true", "1", "yes", "sí", "si")
    return value


class _UsageCarrier(Exception):
    def __init__(self, error: RosError, in_tok: int, out_tok: int, model: str, cost: float | None = None):
        super().__init__(str(error))
        self.error, self.in_tok, self.out_tok, self.model, self.cost = error, in_tok, out_tok, model, cost


Handler = Callable[[str, str, str, dict], dict]


class FakeLLM(LLM):
    """Deterministic model for tests and offline demos. `handler(purpose, system, user, schema)`."""

    def __init__(self, handler: Handler, model: str = "claude-haiku-4-5"):
        self.handler = handler
        self.model = model
        self.calls: list[tuple[str, str]] = []

    def _call(self, *, purpose: str, system: str, user: str, schema: dict, max_tokens: int
              ) -> tuple[dict, int, int, str]:
        self.calls.append((purpose, user))
        data = self.handler(purpose, system, user, schema)
        return data, estimate_tokens(system) + estimate_tokens(user), estimate_tokens(json.dumps(data)), self.model
