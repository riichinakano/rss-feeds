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
    value = value.strip().replace("Z", "+00:00").replace("/", "-").replace(".", "-")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=JST)


def page_url(url, page):
    if page == 1:
        return url
    return f"{url}{'&' if '?' in url else '?'}page={page}"


def short_date(dt):
    """会期表示用。今年なら 10/24、それ以外は 2027/1/30。"""
    local = dt.astimezone(JST)
    if local.year == datetime.now(JST).year:
        return f"{local.month}/{local.day}"
    return f"{local.year}/{local.month}/{local.day}"


def make_label(date, start, end):
    """記事タイトルの前に付ける [ ] の中身。会期があれば会期、なければ掲載日時。"""
    if start:
        return f"{short_date(start)}〜{short_date(end)}" if end else f"{short_date(start)}〜"
    if date:
        local = date.astimezone(JST)
        return f"{local.month}/{local.day} {local.hour}:{local.minute:02d}"
    return None


def fetch(url, attempts=3):
    """一時的なエラー（502など）に備えて、間をあけて再試行する。"""
    for n in range(1, attempts + 1):
        try:
            res = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
            res.raise_for_status()
            return res
        except requests.RequestException as e:
            if n == attempts:
                raise
            print(f"  再試行 {n}/{attempts - 1}: {e}", file=sys.stderr)
            time.sleep(10 * n)


def extract_page(site, url, items, seen_links):
    res = fetch(url)
    soup = BeautifulSoup(res.content, "html.parser")

    added = 0
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
        start = parse_date(select_value(el, site["start"])) if site.get("start") else None
        end = parse_date(select_value(el, site["end"])) if site.get("end") else None
        extra = [v for v in (select_value(el, s) for s in site.get("extra", [])) if v]
        items.append({
            "title": title, "link": link, "date": date, "start": start,
            "label": make_label(date, start, end), "extra": extra,
        })
        added += 1
    return added


def extract(site):
    """一覧ページを取得する。pages を指定すると、空のページに当たるまで順に読む。"""
    items = []
    seen_links = set()
    for page in range(1, site.get("pages", 1) + 1):
        if page > 1:
            time.sleep(3)
        if not extract_page(site, page_url(site["url"], page), items, seen_links):
            break
    return items[:site.get("max_items", 1000)]


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
            {"title": i["title"], "link": i["link"], "label": i["label"], "extra": i["extra"]}
            for i in new_items
        ],
    }


def make_first_digest(site, count):
    """初回は掲載中の記事を記録するだけにし、その旨を1件だけ出す（フィードを空にしないため）。"""
    now = datetime.now(timezone.utc)
    local = now.astimezone(JST)
    return {
        "guid": f"{site['id']}-{now.strftime('%Y%m%dT%H%M%SZ')}",
        "title": f"{site['name']}(取得開始 {local.year}年{local.month}月{local.day}日)",
        "date": now.isoformat(),
        "items": [],
        "note": f"掲載中の{count}件を記録しました。次回の更新から、新しく掲載されたものだけをお届けします。",
    }


def digest_html(digest):
    parts = []
    if digest.get("note"):
        parts.append(f"<p>{escape(digest['note'])}</p>")
    for item in digest["items"]:
        title = escape(item["title"])
        label = item.get("label")
        if not label and item.get("date"):  # 旧形式のstate
            label = make_label(datetime.fromisoformat(item["date"]), None, None)
        if label:
            title = f"[{escape(label)}] {title}"
        lines = [title] + [escape(e) for e in item.get("extra", [])]
        lines.append(f'<a href="{escape(item["link"])}">{escape(item["link"])}</a>')
        parts.append(f"<p>{'<br>'.join(lines)}</p>")
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

    state_exists = (STATE_DIR / f"{site['id']}.json").exists()
    state = load_state(site["id"])
    seen = set(state["seen"])
    new_items = [i for i in items if i["link"] not in seen]
    if site.get("order") == "start":
        far = datetime.max.replace(tzinfo=timezone.utc)
        new_items.sort(key=lambda i: i["start"] or far)

    if not state_exists:
        digest = make_first_digest(site, len(items))
    elif new_items:
        digest = make_digest(site, new_items)
    else:
        digest = None

    print(f"[{site['id']}] 抽出 {len(items)} 件 / 新着 {len(new_items)} 件" + ("（初回のため記録のみ）" if not state_exists else ""))
    if dry_run:
        for i in (new_items if state_exists else items):
            print(f"  [{i['label'] or '-'}] {i['title'][:60]}")
            for e in i["extra"]:
                print(f"      {e}")
            print(f"      {i['link']}")
        if digest:
            print(f"  → まとめ記事: {digest['title']}")
        return

    if digest:
        state["digests"] = [digest] + state["digests"][:MAX_DIGESTS - 1]
    # 一覧に今載っている記事は必ず覚えておく（上限で忘れて再び新着扱いになるのを防ぐ）
    current = [i["link"] for i in items]
    rest = [u for u in state["seen"] if u not in set(current)]
    state["seen"] = (current + rest)[:max(MAX_SEEN, len(current))]
    save_state(site["id"], state)
    write_feed(site, state["digests"])


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
