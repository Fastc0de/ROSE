"""Web search backends.

The search endpoint itself is user-configured (trusted), so it uses a plain client;
every *result URL* still goes through SafeFetcher before being downloaded.
"""

from __future__ import annotations

import html
import re
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

from ..errors import ErrorKind, RosError
from ..security import SafeFetcher, canonical_url
from .base import Capabilities, ConnectorStatus, NormalizedItem, SearchHit, SourceSpec, SyncResult


def _status_error(resp: httpx.Response, name: str) -> RosError | None:
    if resp.status_code == 429:
        return RosError(ErrorKind.RATE_LIMITED, f"{name}: rate limited")
    if resp.status_code in (401, 403):
        return RosError(ErrorKind.INVALID_CREDENTIALS if name != "duckduckgo" else ErrorKind.PLATFORM_BLOCKED,
                        f"{name}: HTTP {resp.status_code}")
    if resp.status_code >= 500:
        return RosError(ErrorKind.TRANSIENT, f"{name}: HTTP {resp.status_code}")
    if resp.status_code >= 400:
        return RosError(ErrorKind.SOURCE_UNREACHABLE, f"{name}: HTTP {resp.status_code}")
    return None


class _HTTPBackend:
    name = "base"

    def __init__(self, client: httpx.Client | None = None, timeout: float = 20.0):
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=True,
                                             headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) ROS/0.1"})

    def _get(self, url: str, **kwargs: Any) -> httpx.Response:
        try:
            resp = self.client.get(url, **kwargs)
        except httpx.TimeoutException as exc:
            raise RosError(ErrorKind.TRANSIENT, f"{self.name}: timeout") from exc
        except httpx.TransportError as exc:
            raise RosError(ErrorKind.SOURCE_UNREACHABLE, f"{self.name}: {exc}") from exc
        err = _status_error(resp, self.name)
        if err:
            raise err
        return resp


class DuckDuckGoBackend(_HTTPBackend):
    name = "duckduckgo"
    _result = re.compile(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
    _snippet = re.compile(r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', re.S)

    def search(self, query: str, limit: int) -> list[SearchHit]:
        resp = self._get("https://html.duckduckgo.com/html/", params={"q": query})
        body = resp.text
        if "anomaly" in body.lower() and "result__a" not in body:
            raise RosError(ErrorKind.PLATFORM_BLOCKED, "duckduckgo: bot challenge; configure brave or searxng")
        links = self._result.findall(body)
        snippets = self._snippet.findall(body)
        hits = []
        for i, (href, title) in enumerate(links):
            url = html.unescape(href)
            if url.startswith("//"):
                url = "https:" + url
            if "duckduckgo.com/l/" in url:
                q = parse_qs(urlsplit(url).query)
                url = unquote(q.get("uddg", [url])[0])
            if "duckduckgo.com/y.js" in url:  # ads
                continue
            hits.append(SearchHit(url, _strip(title), _strip(snippets[i]) if i < len(snippets) else ""))
            if len(hits) >= limit:
                break
        return hits


class BraveBackend(_HTTPBackend):
    name = "brave"

    def __init__(self, api_key: str, client: httpx.Client | None = None):
        super().__init__(client)
        if not api_key:
            raise RosError(ErrorKind.INVALID_CREDENTIALS, "brave: falta BRAVE_API_KEY")
        self.api_key = api_key

    def search(self, query: str, limit: int) -> list[SearchHit]:
        resp = self._get("https://api.search.brave.com/res/v1/web/search",
                         params={"q": query, "count": min(limit, 20)},
                         headers={"X-Subscription-Token": self.api_key, "Accept": "application/json"})
        results = resp.json().get("web", {}).get("results", [])
        return [SearchHit(r["url"], _strip(r.get("title", "")), _strip(r.get("description", ""))) for r in results][:limit]


class SearxngBackend(_HTTPBackend):
    name = "searxng"

    def __init__(self, base_url: str, client: httpx.Client | None = None):
        super().__init__(client)
        self.base_url = base_url.rstrip("/")

    def search(self, query: str, limit: int) -> list[SearchHit]:
        resp = self._get(f"{self.base_url}/search", params={"q": query, "format": "json"})
        results = resp.json().get("results", [])
        return [SearchHit(r["url"], _strip(r.get("title", "")), _strip(r.get("content", ""))) for r in results][:limit]


class StaticSearchBackend:
    """Test/offline backend: maps query substrings to fixed hits."""

    name = "static"

    def __init__(self, fn: Callable[[str, int], list[SearchHit]]):
        self.fn = fn
        self.queries: list[str] = []

    def search(self, query: str, limit: int) -> list[SearchHit]:
        self.queries.append(query)
        return self.fn(query, limit)[:limit]


def _strip(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", fragment)).split())


class SearchWatchConnector:
    """Keyword/topic watch: runs a web search and treats unseen result URLs as new items."""

    connector_id = "search"
    access_mode = "public_web"

    def __init__(self, backend, fetcher: SafeFetcher, max_fetch: int = 5):
        self.backend = backend
        self.fetcher = fetcher
        self.max_fetch = max_fetch

    def capabilities(self) -> Capabilities:
        return Capabilities(self.connector_id, self.access_mode, ("validate", "sync"), cursor=True,
                            notes=f"Búsqueda web vía {self.backend.name}; cobertura limitada a ese buscador.")

    def status(self) -> ConnectorStatus:
        return ConnectorStatus("ready", f"buscador: {self.backend.name}")

    def validate(self, locator: str) -> SourceSpec:
        if not locator.strip():
            raise RosError(ErrorKind.POLICY_REJECTED, "consulta vacía")
        return SourceSpec(self.connector_id, locator.strip(), f"búsqueda: {locator.strip()}")

    def sync(self, spec: SourceSpec, cursor: dict[str, Any] | None) -> SyncResult:
        from .feeds import WebPageConnector
        cursor = dict(cursor or {})
        seen = list(cursor.get("seen", []))
        try:
            hits = self.backend.search(spec.locator, 10)
        except RosError as exc:
            return SyncResult([], cursor, complete=False, error=exc)
        web = WebPageConnector(self.fetcher)
        items, warnings = [], []
        for hit in hits:
            cid = canonical_url(hit.url)
            if cid in seen:
                continue
            seen.append(cid)
            text = hit.snippet
            if len(items) < self.max_fetch:
                try:
                    text = web.fetch(hit.url).text
                except RosError as exc:
                    warnings.append(f"{hit.url}: {exc}")
            items.append(NormalizedItem(external_id=cid, url=hit.url, title=hit.title, text=text,
                                        meta={"query": spec.locator}))
        return SyncResult(items, {"seen": seen[-1000:]}, warnings=warnings)
