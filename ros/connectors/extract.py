"""HTML → readable text with only the standard library."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "aside", "form", "iframe", "template"}
BLOCK = {"p", "div", "section", "article", "li", "h1", "h2", "h3", "h4", "h5", "h6", "br", "tr", "blockquote",
         "pre", "main", "td", "dd", "dt", "figcaption"}


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0
        self.in_title = False
        self.title = ""
        self.meta: dict[str, str] = {}
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag in SKIP:
            self.skip_depth += 1
        elif tag == "title":
            self.in_title = True
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key in {"og:title", "description", "og:description", "article:published_time", "author",
                       "og:site_name", "date", "pubdate"} and a.get("content"):
                self.meta[key] = a["content"].strip()
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])
        if tag in BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP and self.skip_depth:
            self.skip_depth -= 1
        elif tag == "title":
            self.in_title = False
        if tag in BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title += data
        elif not self.skip_depth:
            self.parts.append(data)


def html_to_text(markup: str) -> dict:
    parser = _Extractor()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # malformed markup: keep what was parsed
        pass
    raw = "".join(parser.parts)
    lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in raw.split("\n")]
    # Drop very short boilerplate lines (menus, buttons) but keep headings with some words.
    kept = [ln for ln in lines if len(ln) >= 25 or (len(ln.split()) >= 3)]
    text = "\n".join(kept)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    title = html.unescape(parser.meta.get("og:title") or parser.title).strip()
    return {
        "title": " ".join(title.split())[:300],
        "text": text,
        "description": parser.meta.get("og:description") or parser.meta.get("description"),
        "published_at": parser.meta.get("article:published_time") or parser.meta.get("date") or parser.meta.get("pubdate"),
        "author": parser.meta.get("author"),
        "links": parser.links,
    }
