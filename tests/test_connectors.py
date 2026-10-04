import pytest

from ros.connectors.base import SourceSpec
from ros.connectors.extract import html_to_text
from ros.connectors.feeds import FeedConnector, RedditConnector, WebPageConnector, YouTubeConnector, parse_feed
from ros.errors import ErrorKind, RosError

RSS = """<?xml version="1.0"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>Blog</title>
  <item><title>Primero</title><link>https://blog.example/1</link><guid>g1</guid>
    <pubDate>Mon, 05 Oct 2026 10:00:00 GMT</pubDate><description>&lt;p&gt;Hola &lt;b&gt;mundo&lt;/b&gt;&lt;/p&gt;</description></item>
  <item><title>Segundo</title><link>https://blog.example/2</link><guid>g2</guid><description>texto</description></item>
</channel></rss>"""

ATOM_YOUTUBE = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:media="http://search.yahoo.com/mrss/">
  <title>Canal</title>
  <entry><id>yt:video:abc</id><title>Vídeo nuevo</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=abc"/>
    <published>2026-10-01T12:00:00+00:00</published><author><name>Autora</name></author>
    <media:group><media:description>Descripción del vídeo</media:description></media:group></entry>
</feed>"""

BILLION_LAUGHS = """<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;">]>
<rss><channel><title>&lol2;</title></channel></rss>"""


def test_parse_rss():
    title, items = parse_feed(RSS)
    assert title == "Blog"
    assert [i.external_id for i in items] == ["g1", "g2"]
    assert items[0].text == "Hola mundo"
    assert items[0].published_at.startswith("2026-10-05T10:00:00")


def test_parse_atom_with_media_description():
    title, items = parse_feed(ATOM_YOUTUBE)
    assert title == "Canal"
    item = items[0]
    assert item.url == "https://www.youtube.com/watch?v=abc"
    assert item.text == "Descripción del vídeo" and item.author == "Autora"


def test_parse_feed_rejects_doctype_entities():
    with pytest.raises(RosError) as info:
        parse_feed(BILLION_LAUGHS)
    assert info.value.kind == ErrorKind.UNSUPPORTED_FORMAT


def test_parse_feed_rejects_garbage():
    with pytest.raises(RosError):
        parse_feed("<html>not a feed")


def test_feed_sync_uses_conditional_requests(web, fetcher):
    url = "https://blog.example/feed.xml"
    web.add(url, RSS, content_type="application/rss+xml", headers={"etag": '"v1"'})
    conn = FeedConnector(fetcher)
    spec = conn.validate(url)
    assert spec.label == "Blog"
    first = conn.sync(spec, None)
    assert len(first.items) == 2 and first.cursor["etag"] == '"v1"' and first.cursor["seen"] == ["g1", "g2"]

    web.add(url, "", status=304)
    second = conn.sync(spec, first.cursor)
    assert second.items == [] and second.error is None and second.cursor == first.cursor
    assert web.requests[-1].headers["if-none-match"] == '"v1"'


def test_feed_sync_reports_errors_instead_of_empty(web, fetcher):
    conn = FeedConnector(fetcher)
    result = conn.sync(SourceSpec("rss", "https://blog.example/missing.xml"), {"seen": ["x"]})
    assert result.items == [] and not result.complete
    assert result.error.kind == ErrorKind.CONTENT_DELETED
    assert result.cursor == {"seen": ["x"]}   # cursor untouched on failure


def test_webpage_sync_emits_only_on_change(web, fetcher):
    url = "https://site.example/precios"
    web.html(url, "Precios", ["El plan básico cuesta diez euros al mes en 2026."])
    conn = WebPageConnector(fetcher)
    spec = SourceSpec("web", url)
    first = conn.sync(spec, None)
    assert len(first.items) == 1
    again = conn.sync(spec, first.cursor)
    assert again.items == []
    web.html(url, "Precios", ["El plan básico cuesta doce euros al mes en 2026."])
    changed = conn.sync(spec, again.cursor)
    assert len(changed.items) == 1 and changed.items[0].meta["previous_hash"] == first.cursor["hash"]


@pytest.mark.parametrize("locator,expected", [
    ("r/python", "https://www.reddit.com/r/python/new/.rss"),
    ("https://www.reddit.com/r/LocalLLaMA/", "https://www.reddit.com/r/LocalLLaMA/new/.rss"),
    ("search:baterías de sodio", "https://www.reddit.com/search.rss?q=bater%C3%ADas+de+sodio&sort=new"),
])
def test_reddit_locators(fetcher, locator, expected):
    assert RedditConnector(fetcher).feed_url(locator) == expected


def test_reddit_rejects_invalid_locator(fetcher):
    with pytest.raises(RosError):
        RedditConnector(fetcher).feed_url("no es un subreddit!")


def test_youtube_channel_id_and_handle(web, fetcher):
    yt = YouTubeConnector(fetcher)
    cid = "UC" + "a" * 22
    assert yt.feed_url(f"https://www.youtube.com/channel/{cid}") == \
        f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
    web.add("https://www.youtube.com/@canal", f'<script>{{"channelId":"{cid}"}}</script>')
    assert yt.feed_url("@canal").endswith(cid)


def test_capabilities_are_honest(fetcher):
    for conn in (YouTubeConnector(fetcher), RedditConnector(fetcher)):
        caps = conn.capabilities()
        assert caps.access_mode == "feed"          # never presented as native API access
        assert "API" in caps.notes


def test_html_to_text_drops_boilerplate():
    page = html_to_text("""<html><head><title>T</title><meta property="og:title" content="Título real">
        <script>alert(1)</script></head><body><nav>Inicio Menú</nav>
        <article><p>Este párrafo contiene el contenido principal del artículo.</p></article>
        <footer>Copyright</footer></body></html>""")
    assert page["title"] == "Título real"
    assert "contenido principal" in page["text"]
    assert "alert" not in page["text"] and "Menú" not in page["text"] and "Copyright" not in page["text"]
