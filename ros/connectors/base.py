"""Common connector contract.

Connectors fetch and normalize. They never decide research strategy, persistence
policy, or delivery. Every call returns a typed result, including failures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..errors import RosError


@dataclass
class Capabilities:
    connector_id: str
    access_mode: str              # public_web | feed | official_api
    operations: tuple[str, ...]   # validate, sync, fetch, search
    cursor: bool
    notes: str = ""


STATES = ("unconfigured", "ready", "degraded", "unsupported", "auth_expired", "permission_blocked", "quota_blocked")


@dataclass
class ConnectorStatus:
    """Whether a connector can be selected. Only 'ready' and 'degraded' may run."""
    state: str
    detail: str = ""
    probed_at: str | None = None

    @property
    def usable(self) -> bool:
        return self.state in ("ready", "degraded")


@dataclass
class SourceSpec:
    kind: str          # connector id
    locator: str       # canonical locator (feed URL, page URL, query)
    label: str = ""


@dataclass
class NormalizedItem:
    external_id: str
    url: str | None
    title: str
    text: str
    published_at: str | None = None
    author: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class SyncResult:
    items: list[NormalizedItem]
    cursor: dict[str, Any]
    complete: bool = True
    warnings: list[str] = field(default_factory=list)
    error: RosError | None = None
    bytes: int = 0


@dataclass
class SearchHit:
    url: str
    title: str
    snippet: str


class Connector(Protocol):
    connector_id: str

    def capabilities(self) -> Capabilities: ...
    def status(self) -> ConnectorStatus: ...
    def validate(self, locator: str) -> SourceSpec: ...
    def sync(self, spec: SourceSpec, cursor: dict[str, Any] | None) -> SyncResult: ...


class SearchBackend(Protocol):
    name: str

    def search(self, query: str, limit: int) -> list[SearchHit]: ...
