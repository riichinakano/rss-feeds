"""RSS非対応サイトの一覧ページを読み、新着記事のまとめをRSSとして書き出す。

使い方:
  python build.py                    全サイトを処理
  python build.py --site morningai   1サイトだけ処理
  python build.py --dry-run          抽出結果を表示するだけ（ファイルは書き換えない）
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
SITES_FILE = ROOT / "sites.yaml"
STATE_DIR = ROOT / "state"
DOCS_DIR = ROOT / "docs"
PAGES_BASE = "https://riichinakano.github.io/rss-feeds"

JST = ZoneInfo("Asia/Tokyo")
USER_AGENT = "rss-feeds/1.0 (+https://github.com/riichinakano/rss-feeds)"
MAX_DIGESTS = 14   # フィードに残すまとめ記事の数（1日2回で約1週間）
MAX_SEEN = 500     # 新着判定のために覚えておく掲載済みURLの数
REQUIRED_KEYS = ("id", "name", "url", "item", "title", "link")


def select_value(el, spec):
    """'css' ならテキスト、'css@attr' なら属性値を返す。"""
    css, _, attr = spec.partition("@")
    target = el.select_one(css) if css else el
    if target is None:
        return None
    if attr:
        return target.get(attr)
    return " ".join(target.get_text().split())


def parse_date(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=JST)


def extract(site):
    res = requests.get(site["url"], headers={"User-Agent": USER_AGENT}, timeout=30)
    res.raise_for_status()
    soup = BeautifulSoup(res.content, "html.parser")

    items = []
    seen_links = set()
    for el in soup.select(site["item"]):
        title = select_value(el, site["title"])
        link = select_value(el, site["link"])
        if not title or not link:
            continue
        link = urljoin(site["url"], link)
        if link in seen_links:
            continue
        seen_links.add(link)
        date = parse_date(select_value(el, site["date"])) if site.get("date") else None
        items.append({"title": title, "link": link, "date": date})
        if len(items) >= site.get("max_items", 50):
            break
    return items


def load_state(site_id):
    path = STATE_DIR / f"{site_id}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"seen": [], "digests": []}


def save_state(site_id, state):
    STATE_DIR.mkdir(exist_ok=True)
    path = STATE_DIR / f"{site_id}.json"
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def make_digest(site, new_items):
    dates = [i["date"] for i in new_items if i["date"]]
    updated = max(dates) if dates else datetime.now(timezone.utc)
    local = updated.astimezone(JST)
    return {
        "guid": f"{site['id']}-{updated.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "title": f"{site['name']}({local.year}年{local.month}月{local.day}日 {local.hour}:{local.minute:02d})",
        "date": updated.astimezone(timezone.utc).isoformat(),
        "items": [
            {"title": i["title"], "link": i["link"], "date": i["date"].isoformat() if i["date"] else None}
            for i in new_items
        ],
    }


def digest_html(digest):
    parts = []
    for item in digest["items"]:
        title = escape(item["title"])
        if item.get("date"):
            local = datetime.fromisoformat(item["date"]).astimezone(JST)
            title = f"[{local.month}/{local.day} {local.hour}:{local.minute:02d}] {title}"
        parts.append(f'<p>{title}<br><a href="{escape(item["link"])}">{escape(item["link"])}</a></p>')
    return "\n".join(parts)


def escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def write_feed(site, digests):
    rss = ET.Element("rss", {"version": "2.0", "xmlns:atom": "http://www.w3.org/2005/Atom"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = site["name"]
    ET.SubElement(channel, "link").text = site["url"]
    ET.SubElement(channel, "description").text = site.get("description", f"{site['name']} の新着まとめ")
    ET.SubElement(channel, "language").text = "ja"
    ET.SubElement(channel, "atom:link", {
        "href": f"{PAGES_BASE}/{site['id']}.xml", "rel": "self", "type": "application/rss+xml",
    })
    if digests:
        ET.SubElement(channel, "lastBuildDate").text = format_datetime(datetime.fromisoformat(digests[0]["date"]))

    for d in digests:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = d["title"]
        ET.SubElement(item, "link").text = site["url"]
        ET.SubElement(item, "guid", {"isPermaLink": "false"}).text = d["guid"]
        ET.SubElement(item, "pubDate").text = format_datetime(datetime.fromisoformat(d["date"]))
        ET.SubElement(item, "description").text = digest_html(d)

    DOCS_DIR.mkdir(exist_ok=True)
    ET.indent(rss)
    ET.ElementTree(rss).write(DOCS_DIR / f"{site['id']}.xml", encoding="utf-8", xml_declaration=True)


def process(site, dry_run):
    items = extract(site)
    if not items:
        raise RuntimeError("記事を1件も抽出できませんでした（セレクタがページ構造と合っていない可能性）")

    state = load_state(site["id"])
    seen = set(state["seen"])
    new_items = [i for i in items if i["link"] not in seen]
    digest = make_digest(site, new_items) if new_items else None

    if dry_run:
        print(f"[{site['id']}] 抽出 {len(items)} 件 / 新着 {len(new_items)} 件")
        for i in items:
            mark = "NEW" if i["link"] not in seen else "   "
            date = i["date"].astimezone(JST).strftime("%m-%d %H:%M") if i["date"] else "-----------"
            print(f"  {mark} {date}  {i['title'][:50]}")
            print(f"                     {i['link']}")
        if digest:
            print(f"  → まとめ記事: {digest['title']}")
        return

    if digest:
        state["digests"] = [digest] + state["digests"][:MAX_DIGESTS - 1]
        state["seen"] = ([i["link"] for i in new_items] + state["seen"])[:MAX_SEEN]
        save_state(site["id"], state)
    write_feed(site, state["digests"])
    print(f"[{site['id']}] 抽出 {len(items)} 件 / 新着 {len(new_items)} 件")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", help="処理するサイトのid")
    parser.add_argument("--dry-run", action="store_true", help="抽出結果を表示するだけ")
    args = parser.parse_args()

    sites = yaml.safe_load(SITES_FILE.read_text(encoding="utf-8"))
    if args.site:
        sites = [s for s in sites if s["id"] == args.site]
        if not sites:
            sys.exit(f"sites.yaml に id '{args.site}' がありません")

    failed = []
    for n, site in enumerate(sites):
        missing = [k for k in REQUIRED_KEYS if k not in site]
        if missing:
            failed.append(site.get("id", "?"))
            print(f"[{site.get('id', '?')}] エラー: 必須項目がありません: {', '.join(missing)}", file=sys.stderr)
            continue
        if n:
            time.sleep(3)
        try:
            process(site, args.dry_run)
        except Exception as e:
            failed.append(site["id"])
            print(f"[{site['id']}] エラー: {e}", file=sys.stderr)

    if failed:
        sys.exit(f"失敗したサイト: {', '.join(failed)}")


if __name__ == "__main__":
    main()
