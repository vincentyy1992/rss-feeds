#!/usr/bin/env python3
"""
Generate an RSS feed from the t66y.com index page.

The homepage exposes the latest thread for each forum section. This script turns
those entries into a standard RSS 2.0 feed.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from email.utils import format_datetime
from html.parser import HTMLParser
from typing import Iterable
from xml.etree import ElementTree as ET


DEFAULT_URL = "https://t66y.com/index.php"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@dataclass(frozen=True)
class FeedItem:
    title: str
    link: str
    forum: str
    author: str | None
    pub_date: str | None
    content_html: str | None = None


class ForumRowParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.forum = ""
        self.thread_title = ""
        self.thread_link = ""
        self.author = ""
        self.timestamp = ""

        self._in_forum_heading = False
        self._capture_forum = False
        self._capture_thread = False
        self._capture_author = False
        self._thread_seen = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {name: value or "" for name, value in attrs}

        if tag == "h2":
            self._in_forum_heading = True
            return

        if tag == "a":
            css_class = attrs_dict.get("class", "")
            href = attrs_dict.get("href", "")
            if self._in_forum_heading:
                self._capture_forum = True
            elif "a2" in css_class.split() and href:
                self._thread_seen = True
                self._capture_thread = True
                self.thread_link = urllib.parse.urljoin(self.base_url, href)
            return

        if tag == "span":
            if attrs_dict.get("data-timestamp"):
                self.timestamp = attrs_dict["data-timestamp"]
            elif self._thread_seen and attrs_dict.get("class") == "f12" and not self.author:
                self._capture_author = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "h2":
            self._in_forum_heading = False
        elif tag == "a":
            self._capture_forum = False
            self._capture_thread = False
        elif tag == "span":
            self._capture_author = False

    def handle_data(self, data: str) -> None:
        text = normalize_text(data)
        if not text:
            return
        if self._capture_forum:
            self.forum = join_text(self.forum, text)
        elif self._capture_thread:
            self.thread_title = join_text(self.thread_title, text)
        elif self._capture_author:
            self.author = join_text(self.author, text)

    def item(self) -> FeedItem | None:
        if not self.thread_title or not self.thread_link:
            return None

        return FeedItem(
            title=self.thread_title,
            link=self.thread_link,
            forum=self.forum or "草榴社區",
            author=self.author or None,
            pub_date=format_unix_timestamp(self.timestamp),
        )


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def join_text(left: str, right: str) -> str:
    if not left:
        return right
    return f"{left} {right}"


def format_unix_timestamp(value: str) -> str | None:
    if not value.isdigit():
        return None
    return format_datetime(datetime.fromtimestamp(int(value), timezone.utc))


def fetch_html(url: str, timeout: int, retries: int = 3) -> str:
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
    rows = re.findall(
        r'<tr\s+class=["\']tr3\s+f_one["\'][^>]*>.*?</tr>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )

    items: list[FeedItem] = []
    for row in rows:
        parser = ForumRowParser(base_url)
        parser.feed(row)
        item = parser.item()
        if item:
            items.append(item)

    return items


def enrich_items_with_content(
    items: Iterable[FeedItem], timeout: int, retries: int
) -> list[FeedItem]:
    enriched: list[FeedItem] = []
    for item in items:
        try:
            html = fetch_html(item.link, timeout, retries)
        except Exception as exc:
            print(f"Could not fetch {item.link}: {exc}", file=sys.stderr)
            enriched.append(item)
            continue

        title = parse_thread_title(html) or item.title
        content = parse_thread_content(html, item.link)
        enriched.append(replace(item, title=title, content_html=content))

    return enriched


def parse_thread_title(html: str) -> str | None:
    match = re.search(r"<h4\b[^>]*>(.*?)</h4>", html, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    return html_to_text(match.group(1))


def parse_thread_content(html: str, page_url: str) -> str | None:
    match = re.search(
        r'<div\s+class=["\']tpc_content\s+do_not_catch["\']\s+id=["\']conttpc["\']>'
        r"(.*?)"
        r"(?:\s*<br\s*/?><br\s*/?><div\s+onclick=|\s*<div\s+onclick=|\s*</td>)",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None

    content = clean_content_html(match.group(1), page_url)
    return content or None


def clean_content_html(content: str, page_url: str) -> str:
    content = re.sub(r"(?is)<script\b.*?</script>", "", content)
    content = re.sub(r"\s+on[a-z]+\s*=\s*(['\"]).*?\1", "", content)
    content = normalize_images(content)
    content = absolutize_html_urls(content, page_url)
    content = re.sub(r"\s+", " ", content).strip()
    return content


def normalize_images(content: str) -> str:
    def replace_img(match: re.Match[str]) -> str:
        attrs = match.group(1)
        if not find_attr(attrs, "src"):
            image_url = find_attr(attrs, "ess-data") or find_attr(attrs, "data-link")
            if image_url:
                attrs = f' src="{escape_attr(image_url)}"{attrs}'
        attrs = re.sub(r"\s+iyl-data\s*=\s*(['\"]).*?\1", "", attrs, flags=re.IGNORECASE)
        attrs = re.sub(r"\s+ess-data\s*=\s*(['\"]).*?\1", "", attrs, flags=re.IGNORECASE)
        return f"<img{attrs}>"

    return re.sub(r"<img\b([^>]*)>", replace_img, content, flags=re.IGNORECASE)


def find_attr(attrs: str, name: str) -> str | None:
    match = re.search(
        rf"\b{re.escape(name)}\s*=\s*(['\"])(.*?)\1",
        attrs,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return match.group(2) if match else None


def escape_attr(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def absolutize_html_urls(content: str, page_url: str) -> str:
    def replace_url(match: re.Match[str]) -> str:
        attr, quote, value = match.groups()
        absolute = urllib.parse.urljoin(page_url, value)
        return f"{attr}={quote}{escape_attr(absolute)}{quote}"

    return re.sub(
        r"\b(href|src)\s*=\s*(['\"])(.*?)\2",
        replace_url,
        content,
        flags=re.IGNORECASE | re.DOTALL,
    )


def html_to_text(fragment: str) -> str:
    fragment = re.sub(r"(?is)<script\b.*?</script>", "", fragment)
    fragment = re.sub(r"(?is)<style\b.*?</style>", "", fragment)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    return normalize_text(fragment)


def build_rss(items: Iterable[FeedItem], source_url: str, title: str) -> ET.ElementTree:
    ET.register_namespace("dc", "http://purl.org/dc/elements/1.1/")
    ET.register_namespace("content", "http://purl.org/rss/1.0/modules/content/")
    rss = ET.Element(
        "rss",
        {
            "version": "2.0",
        },
    )
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = title
    ET.SubElement(channel, "link").text = source_url
    ET.SubElement(channel, "description").text = "Latest forum-section updates from t66y.com"
    ET.SubElement(channel, "language").text = "zh-CN"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(
        datetime.now(timezone.utc)
    )

    for item in items:
        node = ET.SubElement(channel, "item")
        ET.SubElement(node, "title").text = f"[{item.forum}] {item.title}"
        ET.SubElement(node, "link").text = item.link
        ET.SubElement(node, "guid", {"isPermaLink": "true"}).text = item.link
        ET.SubElement(node, "category").text = item.forum
        if item.pub_date:
            ET.SubElement(node, "pubDate").text = item.pub_date
        if item.author:
            ET.SubElement(node, "{http://purl.org/dc/elements/1.1/}creator").text = (
                item.author
            )
        summary = f"板块：{item.forum}" + (f"；作者：{item.author}" if item.author else "")
        ET.SubElement(node, "description").text = item.content_html or summary
        if item.content_html:
            ET.SubElement(
                node, "{http://purl.org/rss/1.0/modules/content/}encoded"
            ).text = item.content_html

    return ET.ElementTree(rss)


def write_tree(tree: ET.ElementTree, output: str | None) -> None:
    if output:
        tree.write(output, encoding="utf-8", xml_declaration=True)
        return

    sys.stdout.buffer.write(b'<?xml version="1.0" encoding="utf-8"?>\n')
    ET.indent(tree, space="  ")
    tree.write(sys.stdout.buffer, encoding="utf-8")
    sys.stdout.buffer.write(b"\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate an RSS feed from https://t66y.com/index.php"
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="source index page URL")
    parser.add_argument("-o", "--output", default="rss.xml", help="RSS output path")
    parser.add_argument("--title", default="草榴社區首頁更新", help="RSS channel title")
    parser.add_argument("--limit", type=int, default=0, help="maximum item count")
    parser.add_argument("--timeout", type=int, default=20, help="request timeout seconds")
    parser.add_argument("--retries", type=int, default=3, help="fetch retry count")
    parser.add_argument(
        "--no-content",
        action="store_true",
        help="only include homepage summaries; do not fetch thread pages",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="print RSS to stdout instead of writing --output",
    )
    args = parser.parse_args()

    html = fetch_html(args.url, args.timeout, args.retries)
    items = parse_items(html, args.url)
    if args.limit > 0:
        items = items[: args.limit]
    if not items:
        print("No feed items found. The page structure may have changed.", file=sys.stderr)
        return 2
    if not args.no_content:
        items = enrich_items_with_content(items, args.timeout, args.retries)

    tree = build_rss(items, args.url, args.title)
    ET.indent(tree, space="  ")
    write_tree(tree, None if args.stdout else args.output)
    if not args.stdout:
        print(f"Wrote {len(items)} items to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
