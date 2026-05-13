#!/usr/bin/env python3
"""
Generate an RSS feed from t66y-com.zproxy.org fid=16.

The script reads read.php?tid=... links from the forum listing, fetches each
thread page, extracts the original post body, normalizes lazy image attributes
into regular img src attributes, and writes full-content RSS.
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
from html import escape, unescape
from xml.etree import ElementTree as ET


DEFAULT_URL = "https://t66y-com.zproxy.org/thread0806.php?fid=16"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@dataclass(frozen=True)
class FeedItem:
    title: str
    link: str
    author: str | None
    pub_date: str | None
    likes: str | None
    comments: str | None
    content_html: str | None = None


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


def parse_listing(html: str, base_url: str, include_sticky: bool) -> list[FeedItem]:
    items: list[FeedItem] = []
    seen_tids: set[str] = set()
    for match in re.finditer(
        r"href\s*=\s*['\"](?P<href>/?read\.php\?tid=(?P<tid>\d+)[^'\"]*)['\"]",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        href = unescape(match.group("href"))
        tid = match.group("tid")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query)
        if query.get("page", [""])[0] == "e":
            continue
        if tid in seen_tids:
            continue
        seen_tids.add(tid)

        item = parse_read_link_item(
            html=html,
            anchor_start=match.start(),
            anchor_end=match.end(),
            title_html=extract_anchor_text_at(html, match.start(), match.end()),
            tid=tid,
            base_url=base_url,
        )
        if item and (include_sticky or not is_promoted_item(html, match.start(), item)):
            items.append(item)

    if items:
        return items

    # Fallback for the older mobile mirror shape, kept for compatibility.
    blocks = re.findall(
        r'<div\s+class=["\']list\s+t_one["\'][^>]*>.*?</div>\s*<div\s+class=["\']line["\']',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    seen_links: set[str] = set()
    for block in blocks:
        item = parse_listing_item(block, base_url)
        if not item:
            continue
        if item.link in seen_links:
            continue
        seen_links.add(item.link)
        items.append(item)
    return items


def extract_anchor_text_at(html: str, href_start: int, href_end: int) -> str:
    anchor_start = html.rfind("<a", 0, href_start)
    anchor_close = html.find(">", href_end)
    anchor_end = html.find("</a>", href_end)
    if anchor_start < 0 or anchor_close < 0 or anchor_end < 0:
        return ""
    if ">" in html[anchor_start:href_start]:
        return ""
    return html[anchor_close + 1 : anchor_end]


def parse_read_link_item(
    *,
    html: str,
    anchor_start: int,
    anchor_end: int,
    title_html: str,
    tid: str,
    base_url: str,
) -> FeedItem:
    context = html[max(0, anchor_start - 600) : min(len(html), anchor_end + 600)]
    title = normalize_thread_title(html_to_text(title_html))
    if not title:
        title = extract_title_near_link(html, anchor_start, anchor_end, tid)
    if not title:
        title = f"t66y fid16 thread {tid}"
    timestamp = first_attr(context, "data-timestamp")
    if timestamp:
        timestamp = timestamp.rstrip("s")
    author = extract_author_near_link(context)
    likes = html_to_text(first_match(context, r'<i\b[^>]*class=["\']icon-like["\'][^>]*></i>\s*([^<]+)'))
    comments = html_to_text(first_match(context, r'<i\b[^>]*class=["\']icon-comm["\'][^>]*></i>\s*([^<]+)'))

    link = urllib.parse.urljoin(base_url, f"/read.php?tid={tid}&page=1&fpage=1")
    return FeedItem(
        title=title,
        link=link,
        author=author or None,
        pub_date=format_unix_timestamp(timestamp),
        likes=likes or None,
        comments=comments or None,
    )


def normalize_thread_title(title: str) -> str:
    title = html_to_text(title)
    if re.fullmatch(r"\d+", title):
        return ""
    if re.fullmatch(r"\d{2}-\d{2}\s+\d{2}:\d{2}", title):
        return ""
    if re.fullmatch(r"\d+\s*(?:小時|小时|分鐘|分钟|天前|星期|年前)", title):
        return ""
    if title in {"上一頁", "下一頁", "＜", "＞"}:
        return ""
    return title


def extract_title_near_link(html: str, anchor_start: int, anchor_end: int, tid: str) -> str:
    context = html[max(0, anchor_start - 1500) : min(len(html), anchor_end + 1500)]
    for pattern in (
        rf"<a\b(?=[^>]*\bid=['\"]t{re.escape(tid)}['\"])[^>]*>(?P<title>.*?)</a>",
        rf"<a\b(?=[^>]*href\s*=\s*['\"][^'\"]*/htm(?:_mob|_data)?/[^'\"]*/{re.escape(tid)}\.html['\"])[^>]*>(?P<title>.*?)</a>",
    ):
        match = re.search(pattern, context, flags=re.IGNORECASE | re.DOTALL)
        if match:
            title = normalize_thread_title(match.group("title"))
            if title:
                return title

    titles = []
    for match in re.finditer(
        rf"<a\b(?=[^>]*href\s*=\s*['\"]/?read\.php\?tid={re.escape(tid)}(?:&amp;|&)[^'\"]*)"
        r"[^>]*>(?P<title>.*?)</a>",
        context,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        title = normalize_thread_title(match.group("title"))
        if title:
            titles.append(title)
    if titles:
        return max(titles, key=len)

    candidates = re.findall(
        r"<(?:h1|h2|h3|h4|b)\b[^>]*>(.*?)</(?:h1|h2|h3|h4|b)>",
        context,
        flags=re.IGNORECASE | re.DOTALL,
    )
    for candidate in candidates:
        title = normalize_thread_title(candidate)
        if title:
            titles.append(title)
    if titles:
        return max(titles, key=len)
    return ""


def extract_author_near_link(context: str) -> str:
    author_match = re.search(
        r"<span\b[^>]*>\s*([^<]+?)\s*<span\b[^>]*data-timestamp=",
        context,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if author_match:
        return html_to_text(author_match.group(1))
    author_match = re.search(
        r"<a\b[^>]*class=['\"][^'\"]*\b(?:bl|author)\b[^'\"]*['\"][^>]*>(.*?)</a>",
        context,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return html_to_text(author_match.group(1)) if author_match else ""


def is_promoted_item(html: str, anchor_start: int, item: FeedItem) -> bool:
    context_before = html[max(0, anchor_start - 200) : anchor_start]
    return bool(
        re.search(r"↑\d+", html_to_text(context_before))
        or (item.author and "Top-marks" in item.author)
    )


def parse_listing_item(block: str, base_url: str) -> FeedItem | None:
    anchor = first_match(block, r"<a\b(?=[^>]*href=)[^>]*>.*?</a>")
    href = first_attr(anchor, "href")
    if not href or not re.search(r"/htm(?:_mob)?/", href):
        return None

    title = html_to_text(anchor)
    if not title:
        return None

    detail = first_match(block, r"<BR>\s*<span\b[^>]*class=['\"]f10\s+fl['\"][^>]*>(.*?)</span>")
    author = html_to_text(re.sub(r"<span\b[^>]*data-timestamp=.*", "", detail, flags=re.DOTALL))
    timestamp = first_attr(detail, "data-timestamp")
    if timestamp:
        timestamp = timestamp.rstrip("s")
    likes = html_to_text(first_match(block, r'<i\b[^>]*class=["\']icon-like["\'][^>]*></i>\s*([^<]+)'))
    comments = html_to_text(first_match(block, r'<i\b[^>]*class=["\']icon-comm["\'][^>]*></i>\s*([^<]+)'))

    return FeedItem(
        title=title,
        link=urllib.parse.urljoin(base_url, href),
        author=author or None,
        pub_date=format_unix_timestamp(timestamp),
        likes=likes or None,
        comments=comments or None,
    )


def enrich_items(items: list[FeedItem], timeout: int, retries: int) -> list[FeedItem]:
    enriched: list[FeedItem] = []
    for item in items:
        try:
            html = fetch_html(item.link, timeout, retries)
            refresh_url = parse_meta_refresh_url(html, item.link)
            if refresh_url:
                html = fetch_html(refresh_url, timeout, retries)
        except Exception as exc:
            print(f"Could not fetch {item.link}: {exc}", file=sys.stderr)
            enriched.append(item)
            continue

        title = parse_thread_title(html) or item.title
        content = parse_thread_content(html, item.link)
        enriched.append(replace(item, title=title, content_html=content))
    return enriched


def parse_meta_refresh_url(html: str, page_url: str) -> str | None:
    match = re.search(
        r'<meta\b[^>]*http-equiv=["\']refresh["\'][^>]*content=["\'][^"\']*url=([^"\']+)["\']',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None
    return urllib.parse.urljoin(page_url, unescape(match.group(1).strip()))


def parse_thread_title(html: str) -> str | None:
    return html_to_text(first_match(html, r'<div\b[^>]*class=["\']f18\s+ta["\'][^>]*>(.*?)</div>')) or None


def parse_thread_content(html: str, page_url: str) -> str | None:
    match = re.search(
        r'<div\b[^>]*class=["\']tpc_cont["\'][^>]*id=["\']conttpc["\'][^>]*>'
        r"(.*?)"
        r"(?:<br><br><div\s+onclick=|<div\s+class=[\"']line\s+tpc_line|</div>\s*<div\s+class=[\"']line\s+tpc_line)",
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
    content = append_iina_links(content)
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


def append_iina_links(content: str) -> str:
    video_urls = []
    for url in re.findall(r"https?://[^\s'\"<>]+", unescape(content)):
        clean_url = url.rstrip(").,;，。")
        if re.search(r"\.(?:m3u8|mp4|mov|webm)(?:\?|$)", clean_url, flags=re.IGNORECASE):
            video_urls.append(clean_url)
    for url in dict.fromkeys(video_urls):
        content += f'<p><a href="{escape_attr(make_iina_link(url))}">▶ 用 IINA 打开</a></p>'
    return content


def make_iina_link(url: str) -> str:
    return f"iina://weblink?url={urllib.parse.quote(url, safe='')}"


def first_match(text: str, pattern: str) -> str:
    match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
    return match.group(0 if pattern.startswith("<a") else 1) if match else ""


def first_attr(text: str, attr: str) -> str | None:
    match = re.search(
        rf"\b{re.escape(attr)}\s*=\s*(['\"])(.*?)\1",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return unescape(match.group(2)) if match else None


def find_attr(attrs: str, name: str) -> str | None:
    return first_attr(attrs, name)


def html_to_text(fragment: str) -> str:
    fragment = re.sub(r"(?is)<script\b.*?</script>", "", fragment)
    fragment = re.sub(r"(?is)<style\b.*?</style>", "", fragment)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", unescape(fragment)).strip()


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


def format_unix_timestamp(value: str | None) -> str | None:
    if not value or not value.isdigit():
        return None
    return format_datetime(datetime.fromtimestamp(int(value), timezone.utc))


def build_item_html(item: FeedItem) -> str:
    meta = []
    if item.author:
        meta.append(f"作者：{escape(item.author)}")
    if item.likes:
        meta.append(f"赞：{escape(item.likes)}")
    if item.comments:
        meta.append(f"评论：{escape(item.comments)}")
    meta_html = f"<p>{'；'.join(meta)}</p>" if meta else ""
    body = item.content_html or "<p>未抓到正文，可能需要登录或页面结构已变化。</p>"
    return f"{meta_html}\n{body}\n<p>原文：<a href=\"{escape(item.link)}\">{escape(item.link)}</a></p>"


def build_rss(items: list[FeedItem], source_url: str, title: str) -> ET.ElementTree:
    ET.register_namespace("dc", "http://purl.org/dc/elements/1.1/")
    ET.register_namespace("content", "http://purl.org/rss/1.0/modules/content/")
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = title
    ET.SubElement(channel, "link").text = source_url
    ET.SubElement(channel, "description").text = "t66y fid=16 full-content feed"
    ET.SubElement(channel, "language").text = "zh-CN"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(datetime.now(timezone.utc))

    for item in items:
        content_html = build_item_html(item)
        node = ET.SubElement(channel, "item")
        ET.SubElement(node, "title").text = item.title
        ET.SubElement(node, "link").text = item.link
        ET.SubElement(node, "guid", {"isPermaLink": "true"}).text = item.link
        if item.pub_date:
            ET.SubElement(node, "pubDate").text = item.pub_date
        if item.author:
            ET.SubElement(node, "{http://purl.org/dc/elements/1.1/}creator").text = item.author
        ET.SubElement(node, "category").text = "達蓋爾的旗幟"
        ET.SubElement(node, "description").text = content_html
        ET.SubElement(node, "{http://purl.org/rss/1.0/modules/content/}encoded").text = content_html
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
    parser = argparse.ArgumentParser(description="Generate an RSS feed from t66y fid=16")
    parser.add_argument("--url", default=DEFAULT_URL, help="source forum URL")
    parser.add_argument("-o", "--output", default="t66y_fid16.xml", help="RSS output path")
    parser.add_argument("--title", default="達蓋爾的旗幟", help="RSS channel title")
    parser.add_argument("--limit", type=int, default=25, help="maximum item count")
    parser.add_argument("--timeout", type=int, default=20, help="request timeout seconds")
    parser.add_argument("--retries", type=int, default=3, help="fetch retry count")
    parser.add_argument("--include-sticky", action="store_true", help="include pinned topics")
    parser.add_argument("--no-content", action="store_true", help="do not fetch thread bodies")
    parser.add_argument("--stdout", action="store_true", help="print RSS to stdout")
    args = parser.parse_args()

    html = fetch_html(args.url, args.timeout, args.retries)
    items = parse_listing(html, args.url, args.include_sticky)
    if args.limit > 0:
        items = items[: args.limit]
    if not items:
        print("No feed items found. The page structure may have changed.", file=sys.stderr)
        return 2
    if not args.no_content:
        items = enrich_items(items, args.timeout, args.retries)

    tree = build_rss(items, args.url, args.title)
    write_tree(tree, None if args.stdout else args.output)
    if not args.stdout:
        print(f"Wrote {len(items)} items to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
