#!/usr/bin/env python3
"""
Generate an RSS feed from bad.news listing pages.

The default source is https://bad.news/sort-new because RSS readers generally
expect newest-first feeds. Other listing URLs such as https://bad.news,
https://bad.news/news, or https://bad.news/tag/porn can be passed with --url.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime
from html import escape, unescape
from xml.etree import ElementTree as ET


DEFAULT_URL = "https://bad.news/sort-new"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@dataclass(frozen=True)
class FeedItem:
    title: str
    link: str
    pub_date: str | None
    author: str | None
    category: str | None
    score: str | None
    thumbnail: str | None
    poster: str | None
    video_source: str | None
    video_type: str | None
    download_link: str | None
    content_html: str


def fetch_html(url: str, timeout: int, retries: int) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="replace")
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(attempt)

    assert last_error is not None
    raise last_error


def parse_items(html: str, base_url: str) -> list[FeedItem]:
    chunks = re.findall(
        r'<div\s+class=["\']link\s+show\s*["\'][^>]*data-tid=["\']\d+["\'][^>]*>'
        r".*?"
        r"(?=<div\s+class=[\"']link\s+show\s*[\"'][^>]*data-tid=|<div\s+class=[\"']pagination|$)",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )

    items: list[FeedItem] = []
    seen: set[str] = set()
    for chunk in chunks:
        item = parse_item(chunk, base_url)
        if item and item.link not in seen:
            seen.add(item.link)
            items.append(item)
    return items


def parse_item(chunk: str, base_url: str) -> FeedItem | None:
    link = absolutize(first_attr(chunk, r'<a\b[^>]*class=["\']dateline["\'][^>]*', "href"), base_url)
    if not link:
        link = absolutize(first_attr(chunk, r'<a\b[^>]*class=["\']thumbnail\s*["\'][^>]*', "href"), base_url)
    if not link:
        return None

    title = extract_title(chunk)
    if not title:
        title = link.rstrip("/").rsplit("/", 1)[-1]

    pub_date = parse_datetime(first_attr(chunk, r"<time\b[^>]*", "datetime"))
    author = html_to_text(first_match(chunk, r'<a\b[^>]*class=["\']author["\'][^>]*>(.*?)</a>'))
    category = html_to_text(first_match(chunk, r'<h4\b[^>]*class=["\']label\s+label-info["\'][^>]*>(.*?)</h4>'))
    category = category.replace(" ", "").replace("\n", "") or None
    score = html_to_text(first_match(chunk, r'<div\b[^>]*class=["\']score\s+unvoted["\'][^>]*>(.*?)</div>'))

    thumbnail = absolutize(first_attr(chunk, r"<img\b[^>]*", "data-echo"), base_url)
    poster = absolutize(first_attr(chunk, r"<video\b[^>]*", "poster"), base_url)
    video_source = absolutize(first_attr(chunk, r"<video\b[^>]*", "data-source"), base_url)
    video_type = first_attr(chunk, r"<video\b[^>]*", "data-type")
    download_link = absolutize(
        first_attr(chunk, r'<a\b[^>]*title=["\']点击下载视频["\'][^>]*', "href"),
        base_url,
    )

    return FeedItem(
        title=title,
        link=link,
        pub_date=pub_date,
        author=author or None,
        category=category,
        score=score or None,
        thumbnail=thumbnail,
        poster=poster,
        video_source=video_source,
        video_type=guess_media_type(video_source, video_type) if video_source else None,
        download_link=download_link,
        content_html=build_item_html(
            title=title,
            link=link,
            author=author or None,
            category=category,
            score=score or None,
            thumbnail=thumbnail,
            poster=poster,
            video_source=video_source,
            video_type=guess_media_type(video_source, video_type) if video_source else None,
            download_link=download_link,
        ),
    )


def extract_title(chunk: str) -> str:
    share_title = first_attr(chunk, r'<span\b[^>]*class=["\']share-icon["\'][^>]*', "title")
    title = html_to_text(
        first_match(chunk, r'<a\b[^>]*class=["\']title["\'][^>]*>(.*?)</a>')
    )
    if share_title and (not title or title.lower() == "watch video"):
        return normalize_text(share_title)
    return title or normalize_text(share_title)


def first_match(text: str, pattern: str) -> str:
    match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
    return match.group(1) if match else ""


def first_attr(text: str, tag_pattern: str, attr: str) -> str | None:
    tag_match = re.search(tag_pattern, text, flags=re.IGNORECASE | re.DOTALL)
    if not tag_match:
        return None
    tag = tag_match.group(0)
    attr_match = re.search(
        rf"\b{re.escape(attr)}\s*=\s*(['\"])(.*?)\1",
        tag,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return unescape(attr_match.group(2)) if attr_match else None


def html_to_text(fragment: str) -> str:
    fragment = re.sub(r"(?is)<script\b.*?</script>", "", fragment)
    fragment = re.sub(r"(?is)<style\b.*?</style>", "", fragment)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    return normalize_text(unescape(fragment))


def normalize_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def absolutize(url: str | None, base_url: str) -> str | None:
    if not url:
        return None
    return urllib.parse.urljoin(base_url, url)


def parse_datetime(value: str | None) -> str | None:
    value = normalize_text(value)
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return format_datetime(datetime.strptime(value, fmt).replace(tzinfo=timezone.utc))
        except ValueError:
            pass
    return None


def build_item_html(
    *,
    title: str,
    link: str,
    author: str | None,
    category: str | None,
    score: str | None,
    thumbnail: str | None,
    poster: str | None,
    video_source: str | None,
    video_type: str | None,
    download_link: str | None,
) -> str:
    parts = [f"<p>{escape(title)}</p>"]
    image = poster or thumbnail
    if video_source:
        video_type = video_type or guess_media_type(video_source)
        iina_link = make_iina_link(video_source)
        poster_attr = f' poster="{escape(image)}"' if image else ""
        parts.append(
            f'<p><video controls playsinline preload="metadata" width="100%"{poster_attr}>'
            f'<source src="{escape(video_source)}" type="{escape(video_type)}">'
            f'<a href="{escape(video_source)}">播放视频</a>'
            f'</video></p>'
        )
        parts.append(f'<p><a href="{escape(video_source)}">▶ 在阅读器打开视频</a></p>')
        parts.append(f'<p><a href="{escape(iina_link)}">▶ 用 IINA 打开</a></p>')
    elif image:
        parts.append(f'<p><a href="{escape(link)}"><img src="{escape(image)}" alt=""></a></p>')
    meta = []
    if author:
        meta.append(f"作者：{escape(author)}")
    if category:
        meta.append(f"标签：{escape(category)}")
    if score:
        meta.append(f"得分：{escape(score)}")
    if meta:
        parts.append(f"<p>{'；'.join(meta)}</p>")
    if video_source:
        parts.append(f'<p>视频源：<a href="{escape(video_source)}">{escape(video_source)}</a></p>')
    if download_link:
        parts.append(f'<p>下载视频：<a href="{escape(download_link)}">{escape(download_link)}</a></p>')
    parts.append(f'<p>原文：<a href="{escape(link)}">{escape(link)}</a></p>')
    return "\n".join(parts)


def make_iina_link(url: str) -> str:
    return f"iina://weblink?url={urllib.parse.quote(url, safe='')}"


def guess_media_type(url: str | None, data_type: str | None = None) -> str:
    data_type = normalize_text(data_type).lower()
    if data_type == "m3u8":
        return "application/x-mpegURL"
    if data_type == "mp4":
        return "video/mp4"
    if not url:
        return "video/mp4"
    path = urllib.parse.urlparse(url).path.lower()
    if path.endswith(".m3u8"):
        return "application/x-mpegURL"
    if path.endswith(".mp4"):
        return "video/mp4"
    if path.endswith(".webm"):
        return "video/webm"
    if path.endswith(".mov"):
        return "video/quicktime"
    return "video/mp4"


def build_rss(items: list[FeedItem], source_url: str, title: str) -> ET.ElementTree:
    ET.register_namespace("dc", "http://purl.org/dc/elements/1.1/")
    ET.register_namespace("content", "http://purl.org/rss/1.0/modules/content/")
    ET.register_namespace("media", "http://search.yahoo.com/mrss/")

    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = title
    ET.SubElement(channel, "link").text = source_url
    ET.SubElement(channel, "description").text = "bad.news listing feed"
    ET.SubElement(channel, "language").text = "zh-CN"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(datetime.now(timezone.utc))

    for item in items:
        node = ET.SubElement(channel, "item")
        ET.SubElement(node, "title").text = item.title
        ET.SubElement(node, "link").text = item.link
        ET.SubElement(node, "guid", {"isPermaLink": "true"}).text = item.link
        if item.pub_date:
            ET.SubElement(node, "pubDate").text = item.pub_date
        if item.category:
            ET.SubElement(node, "category").text = item.category
        if item.author:
            ET.SubElement(node, "{http://purl.org/dc/elements/1.1/}creator").text = item.author
        if item.video_source:
            media_type = item.video_type or guess_media_type(item.video_source)
            ET.SubElement(
                node,
                "enclosure",
                {"url": item.video_source, "type": media_type, "length": "1"},
            )
            media_content = ET.SubElement(
                node,
                "{http://search.yahoo.com/mrss/}content",
                {
                    "url": item.video_source,
                    "type": media_type,
                    "medium": "video",
                    "expression": "full",
                    "isDefault": "true",
                },
            )
            thumbnail = item.poster or item.thumbnail
            if thumbnail:
                ET.SubElement(
                    node,
                    "{http://search.yahoo.com/mrss/}thumbnail",
                    {"url": thumbnail},
                )
                ET.SubElement(
                    media_content,
                    "{http://search.yahoo.com/mrss/}thumbnail",
                    {"url": thumbnail},
                )
            ET.SubElement(
                node,
                "{http://search.yahoo.com/mrss/}player",
                {"url": item.video_source},
            )
        elif item.poster or item.thumbnail:
            ET.SubElement(
                node,
                "{http://search.yahoo.com/mrss/}thumbnail",
                {"url": item.poster or item.thumbnail or ""},
            )
        ET.SubElement(node, "description").text = item.content_html
        ET.SubElement(node, "{http://purl.org/rss/1.0/modules/content/}encoded").text = (
            item.content_html
        )

    return ET.ElementTree(rss)


def write_tree(tree: ET.ElementTree, output: str | None) -> None:
    ET.indent(tree, space="  ")
    if output:
        tree.write(output, encoding="utf-8", xml_declaration=True)
        return

    sys.stdout.buffer.write(b'<?xml version="1.0" encoding="utf-8"?>\n')
    tree.write(sys.stdout.buffer, encoding="utf-8")
    sys.stdout.buffer.write(b"\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate an RSS feed from bad.news")
    parser.add_argument("--url", default=DEFAULT_URL, help="source listing URL")
    parser.add_argument("-o", "--output", default="bad_news.xml", help="RSS output path")
    parser.add_argument("--title", default="bad.news 最新", help="RSS channel title")
    parser.add_argument("--limit", type=int, default=0, help="maximum item count")
    parser.add_argument("--timeout", type=int, default=20, help="request timeout seconds")
    parser.add_argument("--retries", type=int, default=3, help="fetch retry count")
    parser.add_argument("--stdout", action="store_true", help="print RSS to stdout")
    args = parser.parse_args()

    html = fetch_html(args.url, args.timeout, args.retries)
    items = parse_items(html, args.url)
    if args.limit > 0:
        items = items[: args.limit]
    if not items:
        print("No feed items found. The page structure may have changed.", file=sys.stderr)
        return 2

    tree = build_rss(items, args.url, args.title)
    write_tree(tree, None if args.stdout else args.output)
    if not args.stdout:
        print(f"Wrote {len(items)} items to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
