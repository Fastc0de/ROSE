"""RSS/Atom connector, plus YouTube and Reddit through their official public feeds.

YouTube channel feeds and Reddit .rss listings are documented, public, feed-level
access. They are reported as access_mode="feed" — never as native API coverage.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote_plus, urlsplit

from ..errors import ErrorKind, RosError
from ..security import SafeFetcher, canonical_url
from .base import Capabilities, NormalizedItem, SourceSpec, SyncResult
from .extract import html_to_text

FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/xml", "text/xml", "text/html",
              "application/rdf+xml", "text/plain")
SEEN_KEEP = 500


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child(el: ET.Element, *names: str) -> ET.Element | None:
    for c in el:
        if _local(c.tag) in names:
            return c
    return None


def _text(el: ET.Element | None) -> str:
    if el is None:
        return ""
    return "".join(el.itertext()).strip()


def _date(value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError):
        return value  # Atom dates are already ISO 8601


def parse_feed(xml_text: str) -> tuple[str, list[NormalizedItem]]:
    xml_text = re.sub(r"<!DOCTYPE[^>]*>", "", xml_text, flags=re.I)  # no DTDs / entity expansion
    try:
        root = ET.fromstring(xml_text.encode("utf-8"))
    except ET.ParseError as exc:
        raise RosError(ErrorKind.UNSUPPORTED_FORMAT, f"not a valid RSS/Atom feed: {exc}") from exc
    kind = _local(root.tag)
    items: list[NormalizedItem] = []
    if kind == "feed":  # Atom (also YouTube, Reddit)
        title = _text(_child(root, "title"))
        entries = [e for e in root if _local(e.tag) == "entry"]
        for e in entries:
            link = ""
            for l in e:
                if _local(l.tag) == "link" and l.get("rel", "alternate") == "alternate":
                    link = l.get("href", "")
                    break
            body = _text(_child(e, "content")) or _text(_child(e, "summary"))
            group = _child(e, "group")  # media:group (YouTube)
            if not body and group is not None:
                body = _text(_child(group, "description"))
            author_el = _child(e, "author")
            items.append(NormalizedItem(
                external_id=_text(_child(e, "id")) or link,
                url=link or None,
                title=_text(_child(e, "title")),
                text=_clean(body),
                published_at=_date(_text(_child(e, "published")) or _text(_child(e, "updated"))),
                author=_text(_child(author_el, "name")) if author_el is not None else None,
            ))
        return title, items
    channel = _child(root, "channel") if kind == "rss" else root
    if channel is None:
        raise RosError(ErrorKind.UNSUPPORTED_FORMAT, f"unknown feed root <{kind}>")
    title = _text(_child(channel, "title"))
    entries = [e for e in (channel if kind == "rss" else root) if _local(e.tag) == "item"]
    for e in entries:
        link = _text(_child(e, "link"))
        body = _text(_child(e, "encoded")) or _text(_child(e, "description"))
        items.append(NormalizedItem(
            external_id=_text(_child(e, "guid")) or link or _text(_child(e, "title")),
            url=link or None,
            title=_text(_child(e, "title")),
            text=_clean(body),
            published_at=_date(_text(_child(e, "pubdate")) or _text(_child(e, "date"))),
            author=_text(_child(e, "creator")) or _text(_child(e, "author")) or None,
        ))
    return title, items


def _clean(body: str) -> str:
    if "<" in body and ">" in body:
        return html_to_text(body)["text"] or re.sub(r"<[^>]+>", " ", body).strip()
    return body


class FeedConnector:
    connector_id = "rss"
    access_mode = "feed"

    def __init__(self, fetcher: SafeFetcher):
        self.fetcher = fetcher

    def capabilities(self) -> Capabilities:
        return Capabilities(self.connector_id, self.access_mode, ("validate", "sync"), cursor=True,
                            notes="Solo cubre lo que publica el feed; no implica cobertura completa del sitio.")

    def feed_url(self, locator: str) -> str:
        return locator.strip()

    def validate(self, locator: str) -> SourceSpec:
        url = self.feed_url(locator)
        resp = self.fetcher.get(url, allowed_types=FEED_TYPES)
        title, _ = parse_feed(resp.text())
        return SourceSpec(self.connector_id, url, title or locator)

    def sync(self, spec: SourceSpec, cursor: dict[str, Any] | None) -> SyncResult:
        cursor = dict(cursor or {})
        headers = {}
        if cursor.get("etag"):
            headers["If-None-Match"] = cursor["etag"]
        if cursor.get("last_modified"):
            headers["If-Modified-Since"] = cursor["last_modified"]
        try:
            resp = self.fetcher.get(spec.locator, headers=headers, allowed_types=FEED_TYPES)
        except RosError as exc:
            return SyncResult([], cursor, complete=False, error=exc)
        if resp.not_modified:
            return SyncResult([], cursor)
        try:
            _, items = parse_feed(resp.text())
        except RosError as exc:
            return SyncResult([], cursor, complete=False, error=exc, bytes=len(resp.body))
        seen = list(cursor.get("seen", []))
        seen_set = set(seen)
        fresh = [i for i in items if i.external_id not in seen_set]
        # Items whose id we've seen are re-emitted only when the ingestion layer must check for edits;
        # to keep cost low we pass everything and let content hashes detect modifications.
        new_cursor = {
            "etag": resp.headers.get("etag"),
            "last_modified": resp.headers.get("last-modified"),
            "seen": (seen + [i.external_id for i in fresh])[-SEEN_KEEP:],
        }
        warnings = ["feed truncated at byte limit"] if resp.truncated else []
        return SyncResult(items, new_cursor, complete=not resp.truncated, warnings=warnings, bytes=len(resp.body))


class YouTubeConnector(FeedConnector):
    """YouTube channel uploads via the official channel feed (no API key needed)."""

    connector_id = "youtube"

    def capabilities(self) -> Capabilities:
        return Capabilities(self.connector_id, self.access_mode, ("validate", "sync"), cursor=True,
                            notes="Feed oficial de subidas del canal (últimos ~15 vídeos). Sin comentarios ni "
                                  "transcripciones: requieren YouTube Data API v3 (no configurada).")

    def feed_url(self, locator: str) -> str:
        loc = locator.strip()
        m = re.search(r"(UC[\w-]{22})", loc)
        if m:
            return f"https://www.youtube.com/feeds/videos.xml?channel_id={m.group(1)}"
        if "feeds/videos.xml" in loc:
            return loc
        handle = loc if loc.startswith("http") else f"https://www.youtube.com/{loc if loc.startswith('@') else '@' + loc}"
        page = self.fetcher.get(handle, allowed_types=("text/html",)).text()
        m = re.search(r'"(?:channelId|externalId)":"(UC[\w-]{22})"', page) or \
            re.search(r"youtube\.com/channel/(UC[\w-]{22})", page)
        if not m:
            raise RosError(ErrorKind.SOURCE_UNREACHABLE, f"no pude resolver el canal de YouTube {locator!r}")
        return f"https://www.youtube.com/feeds/videos.xml?channel_id={m.group(1)}"


class RedditConnector(FeedConnector):
    """Subreddit listings or searches via Reddit's public RSS endpoints."""

    connector_id = "reddit"

    def capabilities(self) -> Capabilities:
        return Capabilities(self.connector_id, self.access_mode, ("validate", "sync"), cursor=True,
                            notes="RSS público de Reddit (posts nuevos, ~25 por consulta). Comentarios completos "
                                  "requieren la API OAuth oficial (no configurada).")

    def feed_url(self, locator: str) -> str:
        loc = locator.strip()
        if loc.startswith("http") and ".rss" in loc:
            return loc
        if loc.lower().startswith("search:"):
            return f"https://www.reddit.com/search.rss?q={quote_plus(loc[7:].strip())}&sort=new"
        m = re.search(r"(?:^|/)r/([A-Za-z0-9_]{2,21})", loc) or re.fullmatch(r"([A-Za-z0-9_]{2,21})", loc)
        if not m:
            raise RosError(ErrorKind.POLICY_REJECTED, f"subreddit no válido: {locator!r} (usa r/nombre o search:texto)")
        return f"https://www.reddit.com/r/{m.group(1)}/new/.rss"


class WebPageConnector:
    """Watches a single public page and emits an item whenever its readable text changes."""

    connector_id = "web"
    access_mode = "public_web"

    def __init__(self, fetcher: SafeFetcher):
        self.fetcher = fetcher

    def capabilities(self) -> Capabilities:
        return Capabilities(self.connector_id, self.access_mode, ("validate", "sync", "fetch"), cursor=True,
                            notes="Detecta cambios en el texto legible de una página pública.")

    def validate(self, locator: str) -> SourceSpec:
        doc = self.fetch(locator)
        return SourceSpec(self.connector_id, canonical_url(locator), doc.title or locator)

    def fetch(self, url: str) -> NormalizedItem:
        resp = self.fetcher.get(url)
        if "html" in resp.content_type or not resp.content_type:
            parsed = html_to_text(resp.text())
        else:
            parsed = {"title": url, "text": resp.text(), "published_at": None, "author": None, "description": None}
        if not parsed["text"].strip():
            raise RosError(ErrorKind.EXTRACTION_INCOMPLETE, f"no readable text at {url}")
        meta = {"truncated": resp.truncated, "final_url": resp.url, "bytes": len(resp.body)}
        return NormalizedItem(external_id=canonical_url(resp.url), url=resp.url, title=parsed["title"] or url,
                              text=parsed["text"], published_at=parsed.get("published_at"),
                              author=parsed.get("author"), meta=meta)

    def sync(self, spec: SourceSpec, cursor: dict[str, Any] | None) -> SyncResult:
        from ..db import text_hash
        cursor = dict(cursor or {})
        headers = {}
        if cursor.get("etag"):
            headers["If-None-Match"] = cursor["etag"]
        if cursor.get("last_modified"):
            headers["If-Modified-Since"] = cursor["last_modified"]
        try:
            resp = self.fetcher.get(spec.locator, headers=headers)
        except RosError as exc:
            return SyncResult([], cursor, complete=False, error=exc)
        if resp.not_modified:
            return SyncResult([], cursor)
        parsed = html_to_text(resp.text()) if "html" in resp.content_type else {"title": spec.locator, "text": resp.text()}
        h = text_hash(parsed["text"])
        new_cursor = {"etag": resp.headers.get("etag"), "last_modified": resp.headers.get("last-modified"), "hash": h}
        if h == cursor.get("hash"):
            return SyncResult([], new_cursor, bytes=len(resp.body))
        item = NormalizedItem(external_id=canonical_url(spec.locator), url=resp.url,
                              title=parsed["title"] or spec.label or spec.locator, text=parsed["text"],
                              meta={"previous_hash": cursor.get("hash")})
        return SyncResult([item], new_cursor, bytes=len(resp.body))


def host_of(url: str | None) -> str:
    if not url:
        return ""
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host
