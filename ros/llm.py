"""Language-model port. Every call returns schema-constrained JSON and is metered.

The model never chooses tools or touches configuration: it only returns data that
the calling code validates and applies within hard limits.
"""

from __future__ import annotations

import json
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

    def complete_json(self, *, purpose: str, system: str, user: str, schema: dict, max_tokens: int,
                      ledger: Ledger | None) -> dict:
        in_price, out_price = price(self.model)
        est_in = estimate_tokens(system) + estimate_tokens(user)
        est_cost = (est_in * in_price + max_tokens * out_price) / 1_000_000
        if ledger:
            # Shrink the output ceiling to what the remaining budget can pay for (never below a useful floor).
            max_tokens, est_cost = ledger.grant_output(est_in, max_tokens, min(MIN_OUTPUT_TOKENS, max_tokens),
                                                       in_price, out_price)
        in_tok = out_tok = 0
        served = self.model
        try:
            data, in_tok, out_tok, served = self._call(purpose=purpose, system=system, user=user, schema=schema,
                                                       max_tokens=max_tokens)
            return data
        finally:
            if ledger:
                # A server-side fallback may have answered with another model: bill at its price.
                s_in, s_out = price(served)
                cost = (in_tok * s_in + out_tok * s_out) / 1_000_000
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
        # Unwrap usage-carrying errors so that tokens spent on failed calls are still billed to the ledger.
        ledger: Ledger | None = kwargs.get("ledger")
        try:
            return super().complete_json(**kwargs)
        except _UsageCarrier as carrier:
            if ledger:
                in_price, out_price = price(carrier.model)
                ledger.record("llm", purpose=kwargs["purpose"] + ":failed", model=carrier.model,
                              input_tokens=carrier.in_tok, output_tokens=carrier.out_tok,
                              cost_usd=(carrier.in_tok * in_price + carrier.out_tok * out_price) / 1_000_000)
            raise carrier.error from None


class _UsageCarrier(Exception):
    def __init__(self, error: RosError, in_tok: int, out_tok: int, model: str):
        super().__init__(str(error))
        self.error, self.in_tok, self.out_tok, self.model = error, in_tok, out_tok, model


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
